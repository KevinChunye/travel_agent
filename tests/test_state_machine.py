import pytest

from src.models.booking import Trip, TripState
from src.services import state as sm


@pytest.fixture
def draft_trip(repo, request_bos_jfk):
    t = Trip(user_id="user1", request=request_bos_jfk)
    repo.save_trip(t)
    return t


def test_happy_path_transitions(repo, draft_trip):
    path = [
        TripState.READY_TO_SEARCH,
        TripState.SEARCHING,
        TripState.OPTIONS_READY,
        TripState.OPTION_SELECTED,
        TripState.REPRICING,
        TripState.AWAITING_BOOKING_CONFIRMATION,
        TripState.BOOKING,
        TripState.CONFIRMED,
        TripState.MONITORING,
        TripState.COMPLETED,
    ]
    for target in path:
        sm.transition(repo, draft_trip, target)
    assert repo.get_trip(draft_trip.id).state == TripState.COMPLETED


def test_illegal_transition_raises_and_does_not_persist(repo, draft_trip):
    with pytest.raises(sm.InvalidTransition):
        sm.transition(repo, draft_trip, TripState.BOOKING)
    assert repo.get_trip(draft_trip.id).state == TripState.DRAFT


def test_cannot_book_without_confirmation_state(repo, draft_trip):
    sm.transition(repo, draft_trip, TripState.READY_TO_SEARCH)
    sm.transition(repo, draft_trip, TripState.SEARCHING)
    sm.transition(repo, draft_trip, TripState.OPTIONS_READY)
    with pytest.raises(sm.InvalidTransition):
        sm.transition(repo, draft_trip, TripState.BOOKING)


def test_exception_states_have_recovery_paths(repo, draft_trip):
    draft_trip.state = TripState.PRICE_CHANGED
    sm.transition(repo, draft_trip, TripState.AWAITING_BOOKING_CONFIRMATION)
    draft_trip.state = TripState.OFFER_EXPIRED
    sm.transition(repo, draft_trip, TripState.SEARCHING)
    draft_trip.state = TripState.BOOKING_FAILED
    sm.transition(repo, draft_trip, TripState.OPTIONS_READY)


def test_terminal_states(repo, draft_trip):
    draft_trip.state = TripState.COMPLETED
    with pytest.raises(sm.InvalidTransition):
        sm.transition(repo, draft_trip, TripState.SEARCHING)
    draft_trip.state = TripState.CANCELLED
    with pytest.raises(sm.InvalidTransition):
        sm.transition(repo, draft_trip, TripState.READY_TO_SEARCH)


def test_state_persisted_in_repository(repo, draft_trip):
    sm.transition(repo, draft_trip, TripState.READY_TO_SEARCH)
    # A fresh read (as another process would do) sees the persisted state:
    # conversation history is never the source of truth.
    assert repo.get_trip(draft_trip.id).state == TripState.READY_TO_SEARCH
