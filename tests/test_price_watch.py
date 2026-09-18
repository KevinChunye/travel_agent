"""Price watches: adaptive scheduling, observations, budget respect."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.models.travel_request import DateRange, TravelRequest
from src.services.price_watch import (
    FINAL_CUTOFF_DAYS,
    PriceWatchService,
    adaptive_interval_days,
)
from src.services.search_budget import (
    BudgetConfig,
    SearchBudgetManager,
    SearchCategory,
)
from tests.conftest import make_offer
from tests.test_monitoring import FakeChannel

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class FakeSearchProvider:
    """Stands in for GoogleFlightsProvider; never touches the network."""

    name = "google_flights"

    def __init__(self, price="200"):
        self.price = price
        self.calls = 0

    def search_with_options(self, request, *, category=None, force_refresh=False,
                            trip_id=None):
        self.calls += 1
        return [make_offer(price=self.price, dep_hour=10,
                           day=request.outbound_date.start)]


def make_watch_request(days_out=45):
    return TravelRequest(
        origin="BOS", destination="LAX",
        outbound_date=DateRange(start=(NOW + timedelta(days=days_out)).date()),
    )


@pytest.fixture
def budget(repo):
    return SearchBudgetManager(
        repo, BudgetConfig(monthly_limit=10, reserve=2), clock=lambda: NOW
    )


@pytest.fixture
def channel():
    return FakeChannel()


def make_service(repo, budget, channel, provider=None, now=NOW):
    return PriceWatchService(
        [provider or FakeSearchProvider()], repo, budget, channel,
        clock=lambda: now,
    )


class TestAdaptiveIntervals:
    def test_bands(self):
        assert adaptive_interval_days(90) == 5
        assert adaptive_interval_days(45) == 3
        assert adaptive_interval_days(20) == 2
        assert adaptive_interval_days(7) == 1
        assert adaptive_interval_days(3) is None
        assert adaptive_interval_days(1) is None


def test_create_watch_persists_and_schedules(repo, budget, channel):
    svc = make_service(repo, budget, channel)
    watch = svc.create_watch("u1", make_watch_request(45), target_price=300,
                             initial_price=350)
    stored = repo.get_watch(watch.id)
    assert stored.target_price == 300
    assert stored.initial_price == stored.lowest_price == 350
    # 45 days out -> 3-day cadence
    assert stored.next_check_at == NOW + timedelta(days=3)
    # Initial observation recorded
    assert len(repo.observations_for_watch(watch.id)) == 1


def test_run_due_records_observation_and_reschedules(repo, budget, channel):
    provider = FakeSearchProvider(price="180")
    svc = make_service(repo, budget, channel, provider)
    watch = svc.create_watch("u1", make_watch_request(45), initial_price=220)
    watch.next_check_at = NOW - timedelta(hours=1)
    repo.save_watch(watch)

    svc.run_due(NOW)
    assert provider.calls == 1
    stored = repo.get_watch(watch.id)
    assert stored.latest_price == 180
    assert stored.lowest_price == 180
    assert stored.next_check_at == NOW + timedelta(days=3)
    obs = repo.observations_for_watch(watch.id)
    assert len(obs) == 2 and obs[-1].best_price == 180
    assert obs[-1].airline is not None
    assert obs[-1].search_fingerprint == watch.search_fingerprint


def test_target_price_notification_fires_once(repo, budget, channel):
    provider = FakeSearchProvider(price="290")
    svc = make_service(repo, budget, channel, provider)
    watch = svc.create_watch("u1", make_watch_request(45), target_price=300,
                             initial_price=380)
    watch.next_check_at = NOW - timedelta(hours=1)
    repo.save_watch(watch)

    notes = svc.run_due(NOW)
    assert len(notes) == 1 and "290" in notes[0]
    assert channel.notifications and "🎯" in channel.notifications[0][1]

    # Second check at the same price: no duplicate notification.
    watch = repo.get_watch(watch.id)
    watch.next_check_at = NOW - timedelta(hours=1)
    repo.save_watch(watch)
    assert svc.run_due(NOW) == []


def test_monitoring_never_uses_reserve(repo, budget, channel):
    provider = FakeSearchProvider()
    svc = make_service(repo, budget, channel, provider)
    watch = svc.create_watch("u1", make_watch_request(45))
    watch.next_check_at = NOW - timedelta(hours=1)
    repo.save_watch(watch)
    # Exhaust everything but the reserve (limit 10, reserve 2).
    for i in range(8):
        budget.record_search(category=SearchCategory.USER_SEARCH,
                             provider="google_flights", fingerprint=f"x{i}")
    svc.run_due(NOW)
    assert provider.calls == 0  # deferred, reserve untouched
    stored = repo.get_watch(watch.id)
    assert stored.active and stored.next_check_at == NOW + timedelta(days=1)


def test_monitoring_stops_near_departure(repo, budget, channel):
    svc = make_service(repo, budget, channel)
    watch = svc.create_watch("u1", make_watch_request(days_out=40))
    # Time passes: departure is now 2 days away.
    later = NOW + timedelta(days=38)
    watch.next_check_at = later - timedelta(hours=1)
    repo.save_watch(watch)
    svc2 = make_service(repo, budget, channel, now=later)
    notes = svc2.run_due(later)
    stored = repo.get_watch(watch.id)
    assert not stored.active
    assert any("ended" in n for n in notes)


def test_stop_watch_and_find_by_code(repo, budget, channel):
    svc = make_service(repo, budget, channel)
    watch = svc.create_watch("u1", make_watch_request(45))
    found = svc.find_watch("u1", "lax")
    assert found is not None and found.id == watch.id
    svc.stop_watch(watch.id)
    assert repo.get_watch(watch.id).active is False
    assert svc.find_watch("u1", "LAX") is None
