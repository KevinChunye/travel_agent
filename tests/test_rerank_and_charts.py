"""Refinements must never call a provider; charts render offline."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models.booking import Trip, TripState
from src.models.watch import PriceObservation, PriceWatch
from src.services.search import SearchService
from src.services.search_budget import search_fingerprint
from tests.conftest import make_offer
from tests.test_price_watch import NOW, make_watch_request


class PoisonProvider:
    """Fails the test if any search/network method is ever invoked."""

    name = "google_flights"

    def supports(self, mode):
        return True

    def search(self, request):
        raise AssertionError("refinement triggered a provider search")

    def search_with_options(self, request, **kwargs):
        raise AssertionError("refinement triggered a provider search")


@pytest.fixture
def trip_with_offers(repo, request_bos_jfk):
    trip = Trip(user_id="u1", request=request_bos_jfk,
                state=TripState.OPTIONS_READY)
    offers = [
        make_offer(price="150", duration=90, dep_hour=9),
        make_offer(price="90", duration=300, connections=1, dep_hour=7),
        make_offer(price="400", duration=70, dep_hour=15, carrier="AA"),
        make_offer(price="260", duration=95, dep_hour=19, carrier="UA"),
    ]
    repo.save_offers(trip.id, offers)
    trip.presented_offer_ids = [o.id for o in offers[:3]]
    repo.save_trip(trip)
    return trip


class TestRefinementsUseNoApi:
    @pytest.mark.parametrize("command", [
        "cheaper", "faster", "earlier", "later", "nonstop only",
        "under 300", "more", "price 80, time 10, convenience 10",
        "price matters more" .replace("matters more", "80"),
    ])
    def test_refine_never_calls_provider(self, repo, trip_with_offers, command):
        svc = SearchService([PoisonProvider()], repo)
        outcome = svc.refine(trip_with_offers, command)
        assert outcome.trip_id == trip_with_offers.id  # completed without API

    def test_under_applies_price_cap(self, repo, trip_with_offers):
        svc = SearchService([PoisonProvider()], repo)
        outcome = svc.refine(trip_with_offers, "under 200")
        prices = [o.ranked.offer.total_price for o in outcome.options]
        assert prices and all(p <= 200 for p in prices)

    def test_weight_change_reranks_locally(self, repo, trip_with_offers):
        svc = SearchService([PoisonProvider()], repo)
        outcome = svc.refine(trip_with_offers, "price 90, time 5, convenience 5")
        # Cheapest offer wins under price-dominant weights.
        assert outcome.options[0].ranked.offer.total_price == min(
            o.ranked.offer.total_price for o in outcome.options
        )


def test_chart_generation(tmp_path, repo):
    request = make_watch_request(45)
    watch = PriceWatch(
        user_id="u1", origin="BOS", destination="LAX",
        request=request, search_fingerprint=search_fingerprint(request),
        target_price=300.0,
    )
    observations = [
        PriceObservation(
            watch_id=watch.id,
            observed_at=NOW - timedelta(days=10 - i),
            best_price=p,
            search_fingerprint=watch.search_fingerprint,
        )
        for i, p in enumerate([380, 360, 365, 340, 310, 295])
    ]
    from src.services.charts import render_price_chart

    out = tmp_path / "chart.png"
    path = render_price_chart(watch, observations, out)
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG magic
    assert len(data) > 5000


def test_chart_requires_observations(tmp_path):
    from src.services.charts import render_price_chart

    request = make_watch_request(45)
    watch = PriceWatch(
        user_id="u1", origin="BOS", destination="LAX",
        request=request, search_fingerprint="fp",
    )
    with pytest.raises(ValueError):
        render_price_chart(watch, [], tmp_path / "x.png")


def test_dashboard_renders(repo):
    from src.services.dashboard import render_travel_page
    from src.services.search_budget import BudgetConfig, SearchBudgetManager

    budget = SearchBudgetManager(repo, BudgetConfig(monthly_limit=250, reserve=25))
    html = render_travel_page(repo, budget)
    assert "Travel dashboard" in html
    assert "SerpAPI usage" in html
    assert "250" in html
