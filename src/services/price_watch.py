"""Price watches: adaptive fare monitoring under a strict API budget.

Watches persist the full search constraints and are re-checked on an
adaptive schedule driven by time-to-departure. Background checks go
through the SearchBudgetManager as PRICE_MONITOR and can never consume
the interactive reserve; when the budget says no, the check is deferred,
never skipped past the limit.

Every check records a PriceObservation, building our own price-history
dataset independent of anything Google returns.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.models.travel_request import TravelRequest
from src.models.watch import PriceObservation, PriceWatch
from src.providers.base import ProviderError
from src.services.search_budget import (
    SearchBudgetManager,
    SearchCategory,
    SearchQuotaExceededError,
    search_fingerprint,
)

logger = logging.getLogger(__name__)

#: Days-to-departure thresholds -> check interval in days.
#: Within FINAL_CUTOFF_DAYS of departure fare monitoring stops.
ADAPTIVE_INTERVALS = ((60, 5), (30, 3), (14, 2), (3, 1))
FINAL_CUTOFF_DAYS = 3
#: When the budget defers a check, retry this much later.
DEFERRAL = timedelta(days=1)


def adaptive_interval_days(days_to_departure: int) -> Optional[int]:
    """5d / 3d / 2d / 1d cadence by proximity; None = stop monitoring."""
    if days_to_departure <= FINAL_CUTOFF_DAYS:
        return None
    for threshold, interval in ADAPTIVE_INTERVALS:
        if days_to_departure > threshold:
            return interval
    return 1


class PriceWatchService:
    def __init__(
        self,
        providers,
        repo,
        budget: Optional[SearchBudgetManager] = None,
        channel=None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._providers = providers
        self._repo = repo
        self._budget = budget
        self._channel = channel
        self._clock = clock

    def _search_provider(self):
        for p in self._providers:
            if hasattr(p, "search_with_options"):
                return p
        return self._providers[0] if self._providers else None

    # -- lifecycle -----------------------------------------------------------

    def create_watch(
        self,
        user_id: str,
        request: TravelRequest,
        *,
        trip_id: Optional[str] = None,
        target_price: Optional[float] = None,
        initial_price: Optional[float] = None,
        currency: str = "USD",
    ) -> PriceWatch:
        now = self._clock()
        watch = PriceWatch(
            user_id=user_id,
            trip_id=trip_id,
            origin=request.origin or "?",
            destination=request.destination or "?",
            request=request,
            search_fingerprint=search_fingerprint(request),
            target_price=target_price,
            initial_price=initial_price,
            lowest_price=initial_price,
            latest_price=initial_price,
            currency=currency,
        )
        watch.next_check_at = self._schedule_next(watch, now)
        self._repo.save_watch(watch)
        if initial_price is not None:
            self._repo.record_observation(
                PriceObservation(
                    watch_id=watch.id,
                    observed_at=now,
                    best_price=initial_price,
                    currency=currency,
                    search_fingerprint=watch.search_fingerprint,
                )
            )
        return watch

    def stop_watch(self, watch_id: str) -> Optional[PriceWatch]:
        watch = self._repo.get_watch(watch_id)
        if watch is None:
            return None
        watch.active = False
        self._repo.save_watch(watch)
        return watch

    def find_watch(self, user_id: str, token: str) -> Optional[PriceWatch]:
        """Match by id, or by origin/destination code (e.g. 'LAX')."""
        token = token.strip().upper()
        for w in self._repo.list_watches(user_id, active_only=True):
            if w.id == token.lower() or token in (w.origin.upper(), w.destination.upper()):
                return w
        return None

    # -- scheduling ----------------------------------------------------------

    def _days_to_departure(self, watch: PriceWatch, now: datetime) -> int:
        if watch.request.outbound_date is None:
            return 9999
        dep = datetime.combine(
            watch.request.outbound_date.start, datetime.min.time(), timezone.utc
        )
        return (dep - now).days

    def _schedule_next(self, watch: PriceWatch, now: datetime) -> Optional[datetime]:
        interval = adaptive_interval_days(self._days_to_departure(watch, now))
        if interval is None:
            return None
        return now + timedelta(days=interval)

    # -- execution -----------------------------------------------------------

    def run_due(self, now: Optional[datetime] = None) -> list[str]:
        """Check every due watch. Returns the notifications sent."""
        now = now or self._clock()
        notifications: list[str] = []
        provider = self._search_provider()
        for watch in self._repo.due_watches(now):
            days_out = self._days_to_departure(watch, now)
            if adaptive_interval_days(days_out) is None:
                watch.active = False
                watch.next_check_at = None
                self._repo.save_watch(watch)
                notifications.append(
                    self._notify(
                        watch.user_id,
                        f"Fare monitoring for {watch.origin}→{watch.destination} "
                        f"ended (departure within {FINAL_CUTOFF_DAYS} days).",
                    )
                )
                continue

            # Budget: background checks never touch the reserve. When the
            # budget says no, defer — never skip past the limit.
            if self._budget is not None and not self._budget.can_search(
                SearchCategory.PRICE_MONITOR
            ):
                watch.next_check_at = now + DEFERRAL
                self._repo.save_watch(watch)
                logger.info("watch %s deferred: monitoring budget exhausted", watch.id)
                continue

            try:
                offers = provider.search_with_options(
                    watch.request,
                    category=SearchCategory.PRICE_MONITOR,
                    trip_id=watch.trip_id,
                )
            except (SearchQuotaExceededError, ProviderError) as exc:
                logger.warning("watch %s check failed: %s", watch.id, exc)
                watch.next_check_at = now + DEFERRAL
                self._repo.save_watch(watch)
                continue

            note = self.record_check(watch, offers, now)
            if note:
                notifications.append(note)
        return notifications

    def record_check(
        self, watch: PriceWatch, offers, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Record an observation from a completed search and notify if the
        target was reached. Split out for testability."""
        now = now or self._clock()
        note: Optional[str] = None
        if offers:
            best = min(offers, key=lambda o: o.total_price)
            price = float(best.total_price)
            self._repo.record_observation(
                PriceObservation(
                    watch_id=watch.id,
                    observed_at=now,
                    best_price=price,
                    currency=best.currency,
                    airline=best.carrier_name or best.carrier,
                    itinerary_id=best.itinerary_fingerprint(),
                    search_fingerprint=watch.search_fingerprint,
                )
            )
            watch.latest_price = price
            if watch.initial_price is None:
                watch.initial_price = price
            if watch.lowest_price is None or price < watch.lowest_price:
                watch.lowest_price = price
            if watch.target_price is not None:
                if price <= watch.target_price and not watch.target_notified:
                    watch.target_notified = True
                    note = self._notify(
                        watch.user_id,
                        f"🎯 {watch.origin}→{watch.destination} dropped to "
                        f"${price:,.0f} — at or below your ${watch.target_price:,.0f} "
                        f"target. Reply 'chart {watch.origin} {watch.destination}' "
                        f"for the history or search again to grab it.",
                    )
                elif price > watch.target_price:
                    watch.target_notified = False
        watch.next_check_at = self._schedule_next(watch, now)
        if watch.next_check_at is None:
            watch.active = False
        self._repo.save_watch(watch)
        return note

    def _notify(self, user_id: str, text: str) -> str:
        if self._channel is not None:
            self._channel.send_notification(user_id, text)
        else:
            logger.info("watch notification for %s: %s", user_id, text)
        return text
