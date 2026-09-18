"""GoogleFlightsProvider: request mapping, normalization, cache, quota.

All tests mock SerpAPI — no live quota is ever consumed here.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from src.models.travel_request import (
    CabinClass,
    DateRange,
    TimeWindow,
    TravelRequest,
)
from src.providers.google_flights import GoogleFlightsProvider
from src.services.search_budget import (
    BudgetConfig,
    SearchBudgetManager,
    SearchCategory,
    SearchQuotaExceededError,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

RESPONSE = {
    "search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?tfs=x"},
    "best_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"id": "BOS", "time": "2026-10-10 15:00"},
                    "arrival_airport": {"id": "ORD", "time": "2026-10-10 16:40"},
                    "airline": "Delta", "flight_number": "DL 123",
                }
            ],
            "total_duration": 160,
            "price": 240,
            "carbon_emissions": {"this_flight": 98000, "difference_percent": -12},
        }
    ],
    "other_flights": [
        {
            "flights": [
                {"departure_airport": {"id": "BOS", "time": "2026-10-10 06:00"},
                 "arrival_airport": {"id": "DTW", "time": "2026-10-10 08:10"},
                 "airline": "Delta", "flight_number": "DL 400"},
                {"departure_airport": {"id": "DTW", "time": "2026-10-10 09:30"},
                 "arrival_airport": {"id": "ORD", "time": "2026-10-10 10:15"},
                 "airline": "Delta", "flight_number": "DL 401"},
            ],
            "layovers": [{"id": "DTW", "duration": 80}],
            "total_duration": 315,
            "price": 180,
        }
    ],
}


def make_request(round_trip=True, **kwargs):
    defaults = dict(
        origin="BOS", destination="ORD",
        outbound_date=DateRange(start=date(2026, 10, 10)),
    )
    if round_trip:
        defaults["return_date"] = DateRange(start=date(2026, 10, 13))
    defaults.update(kwargs)
    return TravelRequest(**defaults)


@pytest.fixture
def budget(repo):
    return SearchBudgetManager(
        repo, BudgetConfig(monthly_limit=10, reserve=2), clock=lambda: NOW
    )


@pytest.fixture
def provider(repo, budget, monkeypatch):
    p = GoogleFlightsProvider(
        api_key="test", repo=repo, budget=budget, cache_ttl_minutes=60,
        clock=lambda: NOW,
    )
    p.calls = []

    def fake_get(params):
        p.calls.append(params)
        return RESPONSE

    monkeypatch.setattr(p, "_get", fake_get)
    return p


class TestRequestMapping:
    def test_round_trip_params(self, provider):
        provider.search(make_request())
        params = provider.calls[0]
        assert params["type"] == 1
        assert params["return_date"] == "2026-10-13"
        assert params["outbound_date"] == "2026-10-10"
        assert params["travel_class"] == 1

    def test_one_way_params(self, provider):
        provider.search(make_request(round_trip=False))
        params = provider.calls[0]
        assert params["type"] == 2
        assert "return_date" not in params

    def test_cabin_passengers_stops_and_time_window(self, provider):
        req = make_request(
            cabin=CabinClass.BUSINESS,
            passengers=2,
            outbound_window=TimeWindow(earliest=time(15, 0)),
        )
        req.constraints.nonstop_only = True
        provider.search(req)
        params = provider.calls[0]
        assert params["travel_class"] == 3
        assert params["adults"] == 2
        assert params["stops"] == 1  # SerpAPI: 1 == nonstop
        assert params["outbound_times"] == "15,23"


class TestNormalization:
    def test_offers_normalized(self, provider):
        offers = provider.search(make_request())
        assert len(offers) == 2
        nonstop, onestop = offers
        assert nonstop.total_price == Decimal("240")
        assert nonstop.connections == 0
        assert nonstop.carrier == "DL"
        assert nonstop.booking_url.startswith("https://www.google.com/")
        assert nonstop.provider_meta["emissions_g"] == 98000
        assert onestop.connections == 1
        assert any("Layover in DTW (80 min)" in r for r in onestop.restrictions)
        assert len(onestop.segments) == 2

    def test_raw_payload_not_exposed(self, provider):
        offers = provider.search(make_request())
        for o in offers:
            assert "best_flights" not in str(o.provider_meta)
            assert set(o.provider_meta) <= {
                "search_params", "emissions_g", "emissions_vs_typical_pct",
            }


class TestCacheAndQuota:
    def test_cache_hit_costs_nothing(self, provider, budget):
        provider.search(make_request())
        provider.search(make_request())  # equivalent -> cache
        assert len(provider.calls) == 1
        usage = budget.get_usage()
        assert usage.used == 1 and usage.cache_hits == 1

    def test_cache_miss_on_different_search(self, provider):
        provider.search(make_request())
        provider.search(make_request(round_trip=False))
        assert len(provider.calls) == 2

    def test_cache_expires(self, provider, repo, budget, monkeypatch):
        provider.search(make_request())
        later = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)  # ttl=60m
        provider._clock = lambda: later
        provider.search(make_request())
        assert len(provider.calls) == 2

    def test_force_refresh_bypasses_cache(self, provider):
        provider.search(make_request())
        provider.search_with_options(make_request(), force_refresh=True)
        assert len(provider.calls) == 2

    def test_quota_exhaustion_raises_without_calling_api(self, provider, budget):
        for i in range(10):
            budget.record_search(
                category=SearchCategory.USER_SEARCH,
                provider="google_flights", fingerprint=f"x{i}",
            )
        with pytest.raises(SearchQuotaExceededError):
            provider.search_with_options(make_request(round_trip=False))
        assert provider.calls == []  # never touched the network

    def test_background_category_respects_reserve(self, provider, budget):
        for i in range(8):  # limit 10, reserve 2 -> background floor reached
            budget.record_search(
                category=SearchCategory.USER_SEARCH,
                provider="google_flights", fingerprint=f"x{i}",
            )
        with pytest.raises(SearchQuotaExceededError):
            provider.search_with_options(
                make_request(round_trip=False),
                category=SearchCategory.PRICE_MONITOR,
            )
        # Interactive search may still use the reserve.
        offers = provider.search_with_options(make_request(round_trip=False))
        assert offers
