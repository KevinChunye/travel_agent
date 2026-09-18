"""Search orchestration.

Fans a TravelRequest out to every provider that supports the requested
modes, applies hard constraints, ranks deterministically, and picks the
options to present. Also handles conversational refinements ("cheaper",
"earlier", "nonstop only") without restarting the conversation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.models.booking import Trip, TripState
from src.models.offer import Offer
from src.models.preferences import SelectionFeedback, UserPreferences
from src.models.travel_request import PreferenceWeights
from src.providers.base import ProviderError, TravelProvider
from src.ranking.scorer import (
    PresentedOption,
    apply_hard_constraints,
    rank_offers,
    select_presentation,
)
from src.services import state as sm
from src.services.search_budget import SearchQuotaExceededError
from src.storage.repository import Repository

logger = logging.getLogger(__name__)


class SearchOutcome(BaseModel):
    trip_id: str
    options: list[PresentedOption]
    total_offers: int
    filtered_out: int
    state: str = ""
    provider_errors: dict[str, str] = Field(default_factory=dict)
    rejection_summary: dict[str, list[str]] = Field(default_factory=dict)


class SearchService:
    def __init__(self, providers: list[TravelProvider], repo: Repository) -> None:
        self._providers = providers
        self._repo = repo

    # -- weights ------------------------------------------------------------

    def effective_weights(self, trip: Trip) -> PreferenceWeights:
        if trip.request.weights is not None:
            return trip.request.weights
        prefs = self._repo.get_preferences(trip.user_id)
        if prefs is not None:
            return prefs.default_weights()
        return PreferenceWeights()

    # -- search -------------------------------------------------------------

    def search(
        self,
        trip: Trip,
        now: Optional[datetime] = None,
        *,
        force_refresh: bool = False,
    ) -> SearchOutcome:
        if trip.state in (TripState.READY_TO_SEARCH, TripState.OPTIONS_READY,
                          TripState.OFFER_EXPIRED, TripState.NO_RESULTS,
                          TripState.SEARCH_FAILED, TripState.SEARCH_QUOTA_REACHED,
                          TripState.AWAITING_USER_BOOKING,
                          TripState.PRICE_WATCH_ACTIVE):
            sm.transition(self._repo, trip, TripState.SEARCHING)
        elif trip.state != TripState.SEARCHING:
            raise sm.InvalidTransition(trip.state, TripState.SEARCHING)

        offers: list[Offer] = []
        provider_errors: dict[str, str] = {}
        quota_hit = False
        for provider in self._providers:
            if not any(provider.supports(m) for m in trip.request.modes):
                continue
            try:
                if hasattr(provider, "search_with_options"):
                    offers.extend(
                        provider.search_with_options(
                            trip.request,
                            force_refresh=force_refresh,
                            trip_id=trip.id,
                        )
                    )
                else:
                    offers.extend(provider.search(trip.request))
            except SearchQuotaExceededError as exc:
                logger.warning("provider %s quota: %s", provider.name, exc)
                provider_errors[provider.name] = str(exc)
                quota_hit = True
            except ProviderError as exc:
                logger.warning("provider %s failed: %s", provider.name, exc)
                provider_errors[provider.name] = str(exc)

        self._repo.save_offers(trip.id, offers)
        outcome = self._rank_and_present(trip, offers, provider_errors, now)

        # Outcome state: quota beats failure beats empty.
        if not offers and quota_hit:
            sm.transition(self._repo, trip, TripState.SEARCH_QUOTA_REACHED)
        elif not offers and provider_errors:
            sm.transition(self._repo, trip, TripState.SEARCH_FAILED)
        elif not outcome.options:
            sm.transition(self._repo, trip, TripState.NO_RESULTS)
        else:
            sm.transition(self._repo, trip, TripState.OPTIONS_READY)
        outcome.state = trip.state.value
        return outcome

    def _rank_and_present(
        self,
        trip: Trip,
        offers: list[Offer],
        provider_errors: dict[str, str],
        now: Optional[datetime] = None,
        max_options: int = 3,
    ) -> SearchOutcome:
        prefs = self._repo.get_preferences(trip.user_id)
        constrained = apply_hard_constraints(offers, trip.request, now)
        ranked = rank_offers(
            constrained.kept, self.effective_weights(trip), trip.request, prefs
        )
        options = select_presentation(ranked, max_options=max_options)
        trip.presented_offer_ids = [o.ranked.offer.id for o in options]
        self._repo.save_trip(trip)
        return SearchOutcome(
            trip_id=trip.id,
            options=options,
            total_offers=len(offers),
            filtered_out=len(constrained.rejected),
            provider_errors=provider_errors,
            rejection_summary=constrained.rejected,
        )

    # -- refinements ---------------------------------------------------------

    def refine(self, trip: Trip, command: str, now: Optional[datetime] = None) -> SearchOutcome:
        """Adjust constraints/weights from a short command and re-rank the
        already-fetched offers. Never triggers a provider/API call — the
        only exception is the explicit ``refresh`` command."""
        cmd = command.strip().lower()
        max_options = 3
        under = re.match(r"under\s+\$?(\d+)", cmd)
        if cmd == "refresh":
            # The one refinement that costs quota: an explicit fresh lookup.
            return self.search(trip, now, force_refresh=True)
        if cmd == "more":
            max_options = 6
        elif cmd in ("nonstop", "nonstop only", "non-stop", "direct only"):
            trip.request.constraints.nonstop_only = True
        elif under:
            trip.request.constraints.max_price = float(under.group(1))
        elif cmd == "cheaper":
            base = self.effective_weights(trip)
            trip.request.weights = PreferenceWeights.parse_overrides("price 60", base)
        elif cmd == "faster":
            base = self.effective_weights(trip)
            trip.request.weights = PreferenceWeights.parse_overrides("time 60", base)
        elif cmd == "earlier":
            shown = [
                self._repo.get_offer(oid) for oid in trip.presented_offer_ids
            ]
            shown = [o for o in shown if o is not None]
            if shown:
                earliest_shown = min(o.departure for o in shown)
                trip.request.outbound_window.latest = earliest_shown.time()
        elif cmd == "later":
            shown = [
                self._repo.get_offer(oid) for oid in trip.presented_offer_ids
            ]
            shown = [o for o in shown if o is not None]
            if shown:
                latest_shown = max(o.departure for o in shown)
                trip.request.outbound_window.earliest = latest_shown.time()
        else:
            try:
                trip.request.weights = PreferenceWeights.parse_overrides(
                    command, self.effective_weights(trip)
                )
            except ValueError:
                raise ValueError(f"Unrecognized refinement command: {command!r}")

        if trip.state in (TripState.OPTIONS_READY, TripState.NO_RESULTS):
            sm.transition(self._repo, trip, TripState.SEARCHING)
        offers = self._repo.get_offers_for_trip(trip.id)
        outcome = self._rank_and_present(trip, offers, {}, now, max_options=max_options)
        if outcome.options:
            sm.transition(self._repo, trip, TripState.OPTIONS_READY)
        else:
            sm.transition(self._repo, trip, TripState.NO_RESULTS)
        outcome.state = trip.state.value
        return outcome

    # -- selection ------------------------------------------------------------

    def select_option(self, trip: Trip, index_or_offer_id: str) -> Offer:
        """Select by 1-based index into the presented options, or offer id."""
        offer_id: Optional[str] = None
        token = index_or_offer_id.strip()
        if token.isdigit():
            idx = int(token) - 1
            if not (0 <= idx < len(trip.presented_offer_ids)):
                raise ValueError(
                    f"Option {token} does not exist; "
                    f"{len(trip.presented_offer_ids)} option(s) were presented"
                )
            offer_id = trip.presented_offer_ids[idx]
        else:
            offer_id = token
        offer = self._repo.get_offer(offer_id)
        if offer is None:
            raise ValueError(f"Unknown offer {offer_id}")
        sm.transition(self._repo, trip, TripState.OPTION_SELECTED)
        trip.selected_offer_id = offer.id
        self._repo.save_trip(trip)
        self._repo.record_feedback(
            SelectionFeedback(
                user_id=trip.user_id,
                trip_id=trip.id,
                selected_offer_id=offer.id,
                presented_offer_ids=trip.presented_offer_ids,
            )
        )
        return offer


# -- channel-agnostic presentation ------------------------------------------

def format_options(outcome: SearchOutcome, repo: Repository) -> str:
    """Plain-text option list; the messaging channel decides final styling."""
    if not outcome.options:
        if outcome.total_offers == 0 and outcome.provider_errors:
            msgs = "; ".join(outcome.provider_errors.values())
            return (
                "The flight search could not be completed: "
                f"{msgs}. No API quota was wasted on partial results — "
                "try again, or check quota with the usage command."
            )
        lines = [
            "No itineraries matched your requirements. "
            f"({outcome.filtered_out} result(s) were excluded by your constraints:)"
        ]
        reason_counts: dict[str, int] = {}
        for reasons in outcome.rejection_summary.values():
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for reason, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {n} offer(s): {reason}")
        lines.append(
            "Consider relaxing the constraint above (e.g. widen the time "
            "window, allow more stops, or raise the price cap)."
        )
        return "\n".join(lines)
    lines: list[str] = []
    for i, option in enumerate(outcome.options, start=1):
        o = option.ranked.offer
        dur_h, dur_m = divmod(o.duration_minutes, 60)
        stops = "nonstop" if o.connections == 0 else f"{o.connections} stop(s)"
        bags = []
        if o.baggage.carry_on_included:
            bags.append("carry-on")
        if o.baggage.checked_bags_included:
            bags.append(f"{o.baggage.checked_bags_included} checked")
        policy = []
        if o.refundable:
            policy.append("refundable")
        if o.changeable:
            policy.append("changeable")
        d2d = ""
        if o.door_to_door_minutes:
            dh, dm = divmod(o.door_to_door_minutes, 60)
            d2d = f", ~{dh}h{dm:02d} door-to-door"
        lines.append(
            f"{i}. [{option.label.upper()}] {o.mode.value} "
            f"{o.carrier_name or o.carrier} — "
            f"{o.origin} {o.departure:%a %b %d %H:%M} → "
            f"{o.destination} {o.arrival:%H:%M} "
            f"({dur_h}h{dur_m:02d}{d2d}, {stops})\n"
            f"   {o.total_price} {o.currency} · "
            f"bags: {', '.join(bags) or 'none included'} · "
            f"{', '.join(policy) or 'restrictive fare'}\n"
            f"   why: {option.ranked.explanation}"
        )
    booking_url = next(
        (o.ranked.offer.booking_url for o in outcome.options
         if o.ranked.offer.booking_url),
        None,
    )
    if booking_url:
        lines.append(f"Book these fares on Google Flights: {booking_url}")
    lines.append(
        "Reply 1/2/3 to pick, or try: more, cheaper, faster, earlier, "
        "nonstop only, or weights like 'price 60, time 25, convenience 15'."
    )
    return "\n".join(lines)
