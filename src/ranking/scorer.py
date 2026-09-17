"""Deterministic ranking engine.

The LLM never ranks offers numerically. This module:
1. filters offers against hard constraints (with reasons),
2. scores survivors on price / time / convenience / reliability in [0, 1],
3. combines them with normalized user weights into a utility score,
4. picks a small set of meaningfully different options to present,
5. produces explanations from the actual component values.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from src.models.offer import Offer
from src.models.preferences import UserPreferences
from src.models.travel_request import (
    HardConstraints,
    PreferenceWeights,
    TimeWindow,
    TravelRequest,
)


class ConstraintResult(BaseModel):
    kept: list[Offer]
    rejected: dict[str, list[str]] = Field(default_factory=dict)  # offer id -> reasons


def apply_hard_constraints(
    offers: list[Offer],
    request: TravelRequest,
    now: Optional[datetime] = None,
) -> ConstraintResult:
    constraints: HardConstraints = request.effective_constraints()
    window: TimeWindow = request.outbound_window
    kept: list[Offer] = []
    rejected: dict[str, list[str]] = {}

    for offer in offers:
        reasons: list[str] = []
        if offer.is_expired(now):
            reasons.append("offer expired")
        if constraints.max_price is not None and offer.total_price > Decimal(
            str(constraints.max_price)
        ):
            reasons.append(
                f"price {offer.total_price} {offer.currency} exceeds max "
                f"{constraints.max_price:g}"
            )
        max_stops = constraints.effective_max_stops()
        if max_stops is not None and offer.connections > max_stops:
            reasons.append(f"{offer.connections} stop(s) exceeds max {max_stops}")
        if constraints.no_red_eye and offer.is_red_eye():
            reasons.append("red-eye departure excluded")
        if constraints.carry_on_required and not offer.baggage.carry_on_included:
            reasons.append("carry-on bag not included")
        if (
            constraints.checked_bag_required
            and offer.baggage.checked_bags_included < request.baggage.checked_bags
        ):
            reasons.append("required checked baggage not included")
        if constraints.refundable_required and not offer.refundable:
            reasons.append("not refundable")
        if constraints.arrive_by is not None and _naive(offer.arrival) > _naive(
            constraints.arrive_by
        ):
            reasons.append(f"arrives after required {constraints.arrive_by:%H:%M}")
        if constraints.depart_after is not None and _naive(offer.departure) < _naive(
            constraints.depart_after
        ):
            reasons.append(f"departs before required {constraints.depart_after:%H:%M}")
        if not window.is_open() and not window.contains(offer.departure.time()):
            reasons.append("departure outside requested time window")

        if reasons:
            rejected[offer.id] = reasons
        else:
            kept.append(offer)
    return ConstraintResult(kept=kept, rejected=rejected)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


class RankedOffer(BaseModel):
    offer: Offer
    price_score: float
    time_score: float
    convenience_score: float
    reliability_score: float
    utility: float
    explanation: str

    def scores(self) -> dict[str, float]:
        return {
            "price": self.price_score,
            "time": self.time_score,
            "convenience": self.convenience_score,
            "reliability": self.reliability_score,
        }


def _inverse_min_max(value: float, lo: float, hi: float) -> float:
    """1.0 for the best (lowest) value, 0.0 for the worst. Equal -> 1.0."""
    if hi <= lo:
        return 1.0
    return round((hi - value) / (hi - lo), 6)


def _convenience(
    offer: Offer,
    request: TravelRequest,
    prefs: Optional[UserPreferences],
) -> float:
    score = 1.0
    # Connections: each transfer costs comfort.
    score -= min(offer.connections * 0.25, 0.5)
    if offer.self_transfer:
        score -= 0.2
    # Departure-time fit: distance from the middle of the requested window,
    # or from a civilized 10:00 default when no window was given.
    target = _window_midpoint(request.outbound_window) or time(10, 0)
    dep_minutes = offer.departure.hour * 60 + offer.departure.minute
    target_minutes = target.hour * 60 + target.minute
    drift_hours = abs(dep_minutes - target_minutes) / 60
    score -= min(drift_hours * 0.05, 0.3)
    # Preferred airports/stations get a small bonus.
    if prefs and prefs.preferred_airports and offer.origin in prefs.preferred_airports:
        score += 0.1
    if offer.is_red_eye():
        score -= 0.2
    return max(0.0, min(1.0, round(score, 6)))


def _window_midpoint(window: TimeWindow) -> Optional[time]:
    if window.is_open():
        return None
    earliest = window.earliest or time(0, 0)
    latest = window.latest or time(23, 59)
    mid = (
        earliest.hour * 60 + earliest.minute + latest.hour * 60 + latest.minute
    ) // 2
    return time(mid // 60, mid % 60)


def _reliability(offer: Offer, prefs: Optional[UserPreferences]) -> float:
    score = 0.5
    if offer.refundable:
        score += 0.2
    if offer.changeable:
        score += 0.15
    if offer.self_transfer:
        score -= 0.25
    if offer.connections == 0:
        score += 0.1
    if prefs:
        preferred = set(prefs.preferred_airlines) | set(prefs.preferred_rail_services)
        if offer.carrier in preferred or (offer.carrier_name or "") in preferred:
            score += 0.15
    return max(0.0, min(1.0, round(score, 6)))


def rank_offers(
    offers: list[Offer],
    weights: PreferenceWeights,
    request: TravelRequest,
    prefs: Optional[UserPreferences] = None,
) -> list[RankedOffer]:
    if not offers:
        return []
    w = weights.normalized()
    prices = [float(o.total_price) for o in offers]
    times = [float(o.effective_travel_minutes()) for o in offers]
    lo_p, hi_p = min(prices), max(prices)
    lo_t, hi_t = min(times), max(times)

    ranked: list[RankedOffer] = []
    for offer in offers:
        ps = _inverse_min_max(float(offer.total_price), lo_p, hi_p)
        ts = _inverse_min_max(float(offer.effective_travel_minutes()), lo_t, hi_t)
        cs = _convenience(offer, request, prefs)
        rs = _reliability(offer, prefs)
        utility = round(
            w["price"] * ps + w["time"] * ts + w["convenience"] * cs
            + w["reliability"] * rs,
            6,
        )
        ranked.append(
            RankedOffer(
                offer=offer,
                price_score=ps,
                time_score=ts,
                convenience_score=cs,
                reliability_score=rs,
                utility=utility,
                explanation=_explain(offer, ps, ts, cs, rs, w),
            )
        )
    # Deterministic order: utility desc, then price asc, duration asc, id.
    ranked.sort(
        key=lambda r: (
            -r.utility,
            r.offer.total_price,
            r.offer.effective_travel_minutes(),
            r.offer.id,
        )
    )
    return ranked


def _explain(
    offer: Offer,
    ps: float,
    ts: float,
    cs: float,
    rs: float,
    w: dict[str, float],
) -> str:
    parts: list[str] = []
    if ps >= 0.8:
        parts.append("among the cheapest results")
    elif ps <= 0.2:
        parts.append("pricier than most results")
    if ts >= 0.8:
        parts.append("one of the fastest")
    elif ts <= 0.2:
        parts.append("slower than most")
    if offer.connections == 0:
        parts.append("nonstop")
    else:
        parts.append(f"{offer.connections} stop(s)")
    if cs >= 0.7:
        parts.append("convenient departure time")
    if rs >= 0.7:
        parts.append("flexible fare (refund/change friendly)")
    dominant = max(w, key=lambda k: w[k])
    parts.append(
        f"scores: price {ps:.2f}, time {ts:.2f}, convenience {cs:.2f}, "
        f"reliability {rs:.2f} (weighting {dominant} most)"
    )
    return "; ".join(parts)


class PresentedOption(BaseModel):
    label: str  # "best" | "cheapest" | "fastest" | "alternative"
    ranked: RankedOffer


def select_presentation(
    ranked: list[RankedOffer], max_options: int = 3
) -> list[PresentedOption]:
    """Pick up to N meaningfully different options: best match, cheapest,
    fastest — collapsing near-duplicates and backfilling with the next
    best distinct alternatives."""
    if not ranked:
        return []
    chosen: list[PresentedOption] = [PresentedOption(label="best", ranked=ranked[0])]

    def is_dupe(candidate: RankedOffer) -> bool:
        return any(
            candidate.offer.is_essentially_same_as(c.ranked.offer) for c in chosen
        )

    cheapest = min(
        ranked, key=lambda r: (r.offer.total_price, -r.utility, r.offer.id)
    )
    if not is_dupe(cheapest) and len(chosen) < max_options:
        chosen.append(PresentedOption(label="cheapest", ranked=cheapest))

    fastest = min(
        ranked,
        key=lambda r: (r.offer.effective_travel_minutes(), -r.utility, r.offer.id),
    )
    if not is_dupe(fastest) and len(chosen) < max_options:
        chosen.append(PresentedOption(label="fastest", ranked=fastest))

    for candidate in ranked[1:]:
        if len(chosen) >= max_options:
            break
        if not is_dupe(candidate):
            chosen.append(PresentedOption(label="alternative", ranked=candidate))
    return chosen
