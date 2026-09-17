"""Trip state machine.

All state changes go through ``transition``; anything not in
ALLOWED_TRANSITIONS raises. State is persisted via the repository —
conversation history is never the source of truth for booking state.
"""

from __future__ import annotations

from src.models.booking import Trip, TripState
from src.storage.repository import Repository


class InvalidTransition(Exception):
    def __init__(self, current: TripState, target: TripState) -> None:
        super().__init__(f"Illegal state transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


S = TripState

ALLOWED_TRANSITIONS: dict[TripState, set[TripState]] = {
    S.DRAFT: {S.NEEDS_INFORMATION, S.READY_TO_SEARCH, S.CANCELLED},
    S.NEEDS_INFORMATION: {S.NEEDS_INFORMATION, S.READY_TO_SEARCH, S.CANCELLED},
    S.READY_TO_SEARCH: {S.SEARCHING, S.NEEDS_INFORMATION, S.CANCELLED},
    S.SEARCHING: {S.OPTIONS_READY, S.READY_TO_SEARCH, S.CANCELLED},
    # Re-searching (refinements like "cheaper", "nonstop only") loops back.
    S.OPTIONS_READY: {S.OPTION_SELECTED, S.SEARCHING, S.READY_TO_SEARCH, S.CANCELLED},
    S.OPTION_SELECTED: {S.REPRICING, S.OPTIONS_READY, S.CANCELLED},
    S.REPRICING: {
        S.AWAITING_BOOKING_CONFIRMATION,
        S.PRICE_CHANGED,
        S.OFFER_EXPIRED,
        S.CANCELLED,
    },
    S.AWAITING_BOOKING_CONFIRMATION: {
        S.BOOKING,
        S.PRICE_CHANGED,
        S.OFFER_EXPIRED,
        S.OPTIONS_READY,  # user declines and goes back to the list
        S.CANCELLED,
    },
    S.BOOKING: {S.CONFIRMED, S.BOOKING_FAILED, S.PAYMENT_FAILED,
                S.PRICE_CHANGED, S.OFFER_EXPIRED},
    S.CONFIRMED: {S.MONITORING, S.CANCELLED},
    S.MONITORING: {S.TRIP_CHANGED, S.COMPLETED, S.CANCELLED},
    S.COMPLETED: set(),
    # Recovery paths from exceptional states.
    S.OFFER_EXPIRED: {S.SEARCHING, S.OPTIONS_READY, S.REPRICING, S.CANCELLED},
    S.PRICE_CHANGED: {S.AWAITING_BOOKING_CONFIRMATION, S.OPTIONS_READY,
                      S.SEARCHING, S.CANCELLED},
    S.BOOKING_FAILED: {S.OPTIONS_READY, S.SEARCHING, S.CANCELLED},
    S.PAYMENT_FAILED: {S.AWAITING_BOOKING_CONFIRMATION, S.OPTIONS_READY, S.CANCELLED},
    S.TRIP_CHANGED: {S.MONITORING, S.CANCELLED, S.COMPLETED},
    S.CANCELLED: set(),
}


def can_transition(current: TripState, target: TripState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition(repo: Repository, trip: Trip, target: TripState) -> Trip:
    """Validate, apply and persist a state change."""
    if not can_transition(trip.state, target):
        raise InvalidTransition(trip.state, target)
    trip.state = target
    repo.save_trip(trip)
    return trip
