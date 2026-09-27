from datetime import timedelta

from src.channels.base import MessagingChannel
from src.models.booking import Booking, BookingStatus
from src.providers.base import StatusResult, TripStatus
from src.services.monitoring import MonitoringService
from tests.conftest import NOW, make_offer


class FakeChannel(MessagingChannel):
    name = "fake"

    def __init__(self):
        self.notifications: list[tuple[str, str]] = []

    def receive_message(self, raw_payload):
        return []

    def send_message(self, user_id, text):
        self.notifications.append((user_id, text))

    def send_notification(self, user_id, text):
        self.notifications.append((user_id, text))


def make_booking(repo, provider, traveler_hash="h"):
    offer = make_offer(dep_hour=10)
    booking = Booking(
        trip_id="trip_x", user_id="u1", provider=provider.name,
        provider_order_id="ord_test", offer=offer,
        passenger_identity_hash=traveler_hash,
        total_paid=offer.total_price, currency="USD",
    )
    repo.save_booking(booking)
    return booking


def test_tasks_created_for_flight_booking(repo, provider):
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: NOW)
    booking = make_booking(repo, provider)
    tasks = svc.create_tasks_for_booking(booking)
    kinds = sorted(t.kind.value for t in tasks)
    assert kinds == [
        "check_in_reminder", "departure_reminder", "departure_reminder",
        "status_check",
    ]
    assert all(repo.tasks_for_booking(booking.id))


def test_no_notification_without_change(repo, provider):
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: NOW)
    booking = make_booking(repo, provider)
    provider.simulate_status(
        "ord_test",
        StatusResult(
            provider_order_id="ord_test", status=TripStatus.ON_TIME,
            departure=booking.offer.departure, arrival=booking.offer.arrival,
        ),
    )
    svc.create_tasks_for_booking(booking)
    # First run establishes the baseline; second run sees no change.
    assert svc.run_due(NOW) == []
    assert svc.run_due(NOW + timedelta(hours=4)) == []
    status_notes = [n for n in channel.notifications if "Schedule" in n[1]]
    assert status_notes == []


def test_schedule_change_notifies_once(repo, provider):
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: NOW)
    booking = make_booking(repo, provider)
    dep = booking.offer.departure
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.ON_TIME,
                     departure=dep, arrival=booking.offer.arrival),
    )
    svc.create_tasks_for_booking(booking)
    svc.run_due(NOW)  # baseline
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.DELAYED,
                     departure=dep + timedelta(minutes=45),
                     arrival=booking.offer.arrival + timedelta(minutes=45)),
    )
    changes = svc.run_due(NOW + timedelta(hours=4))
    assert len(changes) == 1
    assert changes[0].kind == "delay"
    assert any("departure moved" in n[1] for n in channel.notifications)
    assert repo.get_booking(booking.id).status == BookingStatus.CHANGED


def test_small_delay_is_not_notified(repo, provider):
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: NOW)
    booking = make_booking(repo, provider)
    dep = booking.offer.departure
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.ON_TIME,
                     departure=dep, arrival=booking.offer.arrival),
    )
    svc.create_tasks_for_booking(booking)
    svc.run_due(NOW)
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.DELAYED,
                     departure=dep + timedelta(minutes=10),
                     arrival=booking.offer.arrival + timedelta(minutes=10)),
    )
    assert svc.run_due(NOW + timedelta(hours=4)) == []


def test_cancellation_notifies_and_deactivates(repo, provider):
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: NOW)
    booking = make_booking(repo, provider)
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.ON_TIME,
                     departure=booking.offer.departure,
                     arrival=booking.offer.arrival),
    )
    svc.create_tasks_for_booking(booking)
    svc.run_due(NOW)
    provider.simulate_status(
        "ord_test",
        StatusResult(provider_order_id="ord_test", status=TripStatus.CANCELLED),
    )
    changes = svc.run_due(NOW + timedelta(hours=4))
    assert [c.kind for c in changes] == ["cancellation"]
    assert repo.get_booking(booking.id).status == BookingStatus.CANCELLED
    assert any("CANCELLED" in n[1] for n in channel.notifications)


# -- externally booked trips: time zones, delivery, staleness ---------------

from datetime import datetime, timezone  # noqa: E402

from src.models.monitoring import MonitoringTaskKind  # noqa: E402
from src.models.trip import BookedTrip  # noqa: E402


def _booked(repo, origin, departure):
    trip = BookedTrip(user_id="u1", airline="Test Air", flight_number="TA1",
                      origin=origin, destination="JFK", departure=departure)
    repo.save_booked_trip(trip)
    return trip


def _due(tasks):
    return sorted((t.kind.value, t.due_at) for t in tasks)


def test_reminders_use_origin_airport_time_zone(repo, provider):
    svc = MonitoringService([provider], repo, clock=lambda: NOW)
    # 09:00 in Tokyo is 00:00 UTC (JST has no DST).
    tokyo = svc.create_tasks_for_trip(_booked(repo, "NRT", datetime(2026, 11, 10, 9, 0)))
    assert _due(tokyo) == [
        ("check_in_reminder", datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)),
        ("departure_reminder", datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc)),
        ("departure_reminder", datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)),
    ]
    # 06:30 in Boston during daylight time is 10:30 UTC.
    boston = svc.create_tasks_for_trip(_booked(repo, "BOS", datetime(2026, 10, 10, 6, 30)))
    three_hour = max(t.due_at for t in boston)
    assert three_hour == datetime(2026, 10, 10, 7, 30, tzinfo=timezone.utc)


def test_aware_and_unknown_airport_departures(repo, provider):
    svc = MonitoringService([provider], repo, clock=lambda: NOW)
    aware = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    tasks = svc.create_tasks_for_trip(_booked(repo, "NRT", aware))
    assert max(t.due_at for t in tasks) == datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    unknown = svc.create_tasks_for_trip(_booked(repo, "ZZQ", datetime(2026, 11, 10, 12, 0)))
    assert max(t.due_at for t in unknown) == datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


def test_due_reminders_are_returned_in_outbox(repo, provider):
    trip = _booked(repo, "NRT", datetime(2026, 11, 10, 9, 0))
    MonitoringService([provider], repo, clock=lambda: NOW).create_tasks_for_trip(trip)
    run_at = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)  # 23h before
    svc = MonitoringService([provider], repo, clock=lambda: run_at)
    svc.run_due(run_at)
    assert sorted(n["kind"] for n in svc.outbox) == [
        MonitoringTaskKind.CHECK_IN_REMINDER.value,
        MonitoringTaskKind.DEPARTURE_REMINDER.value,
    ]
    assert all(n["user_id"] == "u1" and "TA1" in n["text"] for n in svc.outbox)


def test_reminders_are_not_sent_after_departure(repo, provider):
    trip = _booked(repo, "NRT", datetime(2026, 11, 10, 9, 0))
    MonitoringService([provider], repo, clock=lambda: NOW).create_tasks_for_trip(trip)
    late = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)  # host slept past departure
    channel = FakeChannel()
    svc = MonitoringService([provider], repo, channel, clock=lambda: late)
    svc.run_due(late)
    assert svc.outbox == [] and channel.notifications == []
    assert not any(t.active for t in repo.tasks_for_booking(trip.id))
