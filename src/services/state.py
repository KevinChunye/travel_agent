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
    S.SEARCHING: {
        S.OPTIONS_READY,
        S.NO_RESULTS,
        S.SEARCH_FAILED,
        S.SEARCH_QUOTA_REACHED,
        S.READY_TO_SEARCH,
        S.CANCELLED,
    },
    # Re-searching (refinements like "cheaper", "nonstop only") loops back.
    S.OPTIONS_READY: {
        S.OPTION_SELECTED,
        S.PRICE_WATCH_ACTIVE,
        S.SEARCHING,
        S.READY_TO_SEARCH,
        S.CANCELLED,
    },
    # Active flow: select -> booking link -> user books externally.
    S.OPTION_SELECTED: {
        S.BOOKING_LINK_READY,
        S.BOOKING_LINK_UNAVAILABLE,
        S.PRICE_WATCH_ACTIVE,
        S.OPTIONS_READY,
        S.CANCELLED,
    },
    S.BOOKING_LINK_READY: {
        S.AWAITING_USER_BOOKING,
        S.OPTIONS_READY,
        S.CANCELLED,
    },
    S.AWAITING_USER_BOOKING: {
        S.TRIP_CONFIRMED,
        S.OPTIONS_READY,
        S.PRICE_WATCH_ACTIVE,
        S.SEARCHING,
        S.CANCELLED,
    },
    S.TRIP_CONFIRMED: {S.MONITORING, S.COMPLETED, S.CANCELLED},
    S.MONITORING: {S.TRIP_CHANGED, S.COMPLETED, S.CANCELLED},
    S.COMPLETED: set(),
    # Price tracking
    S.PRICE_WATCH_ACTIVE: {
        S.TRACKING_PAUSED,
        S.OPTIONS_READY,
        S.SEARCHING,
        S.OPTION_SELECTED,
        S.TRIP_CONFIRMED,
        S.COMPLETED,
        S.CANCELLED,
    },
    S.TRACKING_PAUSED: {S.PRICE_WATCH_ACTIVE, S.CANCELLED, S.COMPLETED},
    # Search-outcome recovery paths.
    S.NO_RESULTS: {S.READY_TO_SEARCH, S.SEARCHING, S.NEEDS_INFORMATION, S.CANCELLED},
    S.SEARCH_FAILED: {S.READY_TO_SEARCH, S.SEARCHING, S.CANCELLED},
    S.SEARCH_QUOTA_REACHED: {S.READY_TO_SEARCH, S.SEARCHING, S.OPTIONS_READY,
                             S.CANCELLED},
    S.BOOKING_LINK_UNAVAILABLE: {S.OPTIONS_READY, S.OPTION_SELECTED,
                                 S.SEARCHING, S.CANCELLED},
    # Exceptional states.
    S.OFFER_EXPIRED: {S.SEARCHING, S.OPTIONS_READY, S.REPRICING, S.CANCELLED},
    S.PRICE_CHANGED: {S.AWAITING_BOOKING_CONFIRMATION, S.OPTIONS_READY,
                      S.SEARCHING, S.CANCELLED},
    S.TRIP_CHANGED: {S.MONITORING, S.CANCELLED, S.COMPLETED},
    S.CANCELLED: set(),
    # ---- LEGACY transactional flow: no active-flow state transitions
    # into it (test-asserted); kept only so historical trip rows in these
    # states remain loadable. The agent never books or takes payment. ----
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
        S.OPTIONS_READY,
        S.CANCELLED,
    },
    S.BOOKING: {S.CONFIRMED, S.BOOKING_FAILED, S.PAYMENT_FAILED,
                S.PRICE_CHANGED, S.OFFER_EXPIRED},
    S.CONFIRMED: {S.MONITORING, S.CANCELLED},
    S.BOOKING_FAILED: {S.OPTIONS_READY, S.SEARCHING, S.CANCELLED},
    S.PAYMENT_FAILED: {S.AWAITING_BOOKING_CONFIRMATION, S.OPTIONS_READY, S.CANCELLED},
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
