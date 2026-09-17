from datetime import timedelta

from src.models.booking import Trip, TripState
from src.models.monitoring import MonitoringTask, MonitoringTaskKind
from src.models.preferences import UserPreferences
from tests.conftest import NOW, make_offer


def test_trip_roundtrip_and_state_persistence(repo, request_bos_jfk):
    trip = Trip(user_id="u1", request=request_bos_jfk)
    repo.save_trip(trip)
    loaded = repo.get_trip(trip.id)
    assert loaded.request.origin == "BOS"
    trip.state = TripState.READY_TO_SEARCH
    repo.save_trip(trip)
    assert repo.get_trip(trip.id).state == TripState.READY_TO_SEARCH
    assert repo.latest_trip_for_user("u1").id == trip.id


def test_offer_roundtrip_preserves_money_and_expiry(repo):
    offer = make_offer(price="123.45", expires_at=NOW)
    repo.save_offers("trip_x", [offer])
    loaded = repo.get_offer(offer.id)
    assert str(loaded.total_price) == "123.45"
    assert loaded.expires_at is not None
    assert loaded.itinerary_fingerprint() == offer.itinerary_fingerprint()


def test_preferences_roundtrip(repo):
    prefs = UserPreferences(
        user_id="u1", home_airport="BOS", preferred_airlines=["B6"],
        price_sensitivity=70,
    )
    repo.save_preferences(prefs)
    loaded = repo.get_preferences("u1")
    assert loaded.home_airport == "BOS"
    assert loaded.default_weights().price == 70
    assert repo.get_preferences("nobody") is None


def test_due_monitoring_tasks_query(repo):
    due = MonitoringTask(
        booking_id="b1", user_id="u1",
        kind=MonitoringTaskKind.STATUS_CHECK, due_at=NOW - timedelta(minutes=1),
    )
    future = MonitoringTask(
        booking_id="b1", user_id="u1",
        kind=MonitoringTaskKind.DEPARTURE_REMINDER,
        due_at=NOW + timedelta(hours=5),
    )
    inactive = MonitoringTask(
        booking_id="b1", user_id="u1",
        kind=MonitoringTaskKind.STATUS_CHECK,
        due_at=NOW - timedelta(hours=1), active=False,
    )
    for t in (due, future, inactive):
        repo.save_monitoring_task(t)
    ids = [t.id for t in repo.due_monitoring_tasks(NOW)]
    assert ids == [due.id]
