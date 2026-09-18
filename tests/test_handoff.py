"""Booking-link handoff and the revised (no-transaction) state machine."""

from datetime import date

import pytest

from src.models.booking import Trip, TripState
from src.models.travel_request import DateRange, TravelRequest
from src.services import state as sm
from src.services.handoff import PAYMENT_DISCLAIMER, HandoffService
from src.services.monitoring import MonitoringService
from tests.conftest import make_offer


@pytest.fixture
def selected_trip(repo, request_bos_jfk):
    trip = Trip(user_id="user1", request=request_bos_jfk,
                state=TripState.OPTION_SELECTED)
    offer = make_offer(price="215", dep_hour=15)
    offer.booking_url = "https://www.google.com/travel/flights?tfs=abc"
    repo.save_offers(trip.id, [offer])
    trip.selected_offer_id = offer.id
    repo.save_trip(trip)
    return trip


def test_booking_link_flow(repo, selected_trip):
    svc = HandoffService(repo)
    result = svc.booking_link(selected_trip)
    assert result.booking_url == "https://www.google.com/travel/flights?tfs=abc"
    assert selected_trip.state == TripState.AWAITING_USER_BOOKING
    assert PAYMENT_DISCLAIMER in result.display_text
    assert "card" in result.display_text  # explicit no-payment warning


def test_booking_link_unavailable(repo, request_bos_jfk):
    trip = Trip(user_id="user1", request=request_bos_jfk,
                state=TripState.OPTION_SELECTED)
    offer = make_offer(price="215")  # no booking_url
    repo.save_offers(trip.id, [offer])
    trip.selected_offer_id = offer.id
    repo.save_trip(trip)
    result = HandoffService(repo).booking_link(trip)
    assert trip.state == TripState.BOOKING_LINK_UNAVAILABLE
    assert result.booking_url is None


def test_mark_booked_creates_trip_and_monitors(repo, selected_trip, provider):
    svc = HandoffService(repo)
    svc.booking_link(selected_trip)
    booked = svc.mark_booked(
        selected_trip, {"confirmation_code": "ABC123"}
    )
    assert selected_trip.state == TripState.MONITORING
    assert booked.confirmation_code == "ABC123"
    assert booked.origin == "BOS" and booked.destination == "JFK"
    stored = repo.get_booked_trip(booked.id)
    assert stored is not None and stored.airline is not None
    # Reminders can be created for externally-booked trips.
    tasks = MonitoringService([provider], repo).create_tasks_for_trip(booked)
    assert len(tasks) == 3
    assert repo.tasks_for_booking(booked.id)


def test_booked_trip_stores_no_payment_fields():
    from src.models.trip import BookedTrip

    fields = set(BookedTrip.model_fields)
    for forbidden in ("card", "cvv", "payment", "passport"):
        assert not any(forbidden in f for f in fields)


class TestRevisedStateMachine:
    def test_active_happy_path(self, repo, request_bos_jfk):
        trip = Trip(user_id="u", request=request_bos_jfk)
        repo.save_trip(trip)
        for target in (
            TripState.READY_TO_SEARCH, TripState.SEARCHING,
            TripState.OPTIONS_READY, TripState.OPTION_SELECTED,
            TripState.BOOKING_LINK_READY, TripState.AWAITING_USER_BOOKING,
            TripState.TRIP_CONFIRMED, TripState.MONITORING,
            TripState.COMPLETED,
        ):
            sm.transition(repo, trip, target)
        assert repo.get_trip(trip.id).state == TripState.COMPLETED

    def test_search_outcome_states_and_recovery(self, repo, request_bos_jfk):
        for outcome in (TripState.NO_RESULTS, TripState.SEARCH_FAILED,
                        TripState.SEARCH_QUOTA_REACHED):
            trip = Trip(user_id="u", request=request_bos_jfk,
                        state=TripState.SEARCHING)
            repo.save_trip(trip)
            sm.transition(repo, trip, outcome)
            sm.transition(repo, trip, TripState.SEARCHING)  # recoverable

    def test_price_watch_states(self, repo, request_bos_jfk):
        trip = Trip(user_id="u", request=request_bos_jfk,
                    state=TripState.OPTIONS_READY)
        repo.save_trip(trip)
        sm.transition(repo, trip, TripState.PRICE_WATCH_ACTIVE)
        sm.transition(repo, trip, TripState.TRACKING_PAUSED)
        sm.transition(repo, trip, TripState.PRICE_WATCH_ACTIVE)
        sm.transition(repo, trip, TripState.TRIP_CONFIRMED)

    def test_legacy_transactional_states_not_reachable_from_active_flow(self):
        # The only bridge into the legacy flow is OPTION_SELECTED->REPRICING
        # (used by the isolated Duffel BookingService). No active-flow state
        # reaches BOOKING/PAYMENT states.
        active = {
            TripState.BOOKING_LINK_READY, TripState.AWAITING_USER_BOOKING,
            TripState.TRIP_CONFIRMED, TripState.PRICE_WATCH_ACTIVE,
            TripState.TRACKING_PAUSED, TripState.NO_RESULTS,
            TripState.SEARCH_FAILED, TripState.SEARCH_QUOTA_REACHED,
        }
        legacy = {TripState.BOOKING, TripState.AWAITING_BOOKING_CONFIRMATION,
                  TripState.PAYMENT_FAILED, TripState.BOOKING_FAILED,
                  TripState.REPRICING}
        for state in active:
            assert not (sm.ALLOWED_TRANSITIONS[state] & legacy), state
