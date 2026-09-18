from datetime import date
from decimal import Decimal

import pytest

from src.models.travel_request import DateRange, TravelRequest
from src.providers.base import OfferNotAvailableError, ProviderNotConfiguredError
from src.providers.serpapi import SerpApiFlightsProvider

SAMPLE_ITEM = {
    "flights": [
        {
            "departure_airport": {"name": "Logan International", "id": "BOS",
                                  "time": "2026-10-01 14:30"},
            "arrival_airport": {"name": "O'Hare International", "id": "ORD",
                                "time": "2026-10-01 16:10"},
            "duration": 160,
            "airline": "United",
            "flight_number": "UA 419",
        }
    ],
    "total_duration": 160,
    "price": 187,
    "type": "Round trip",
}

SAMPLE_RESPONSE = {
    "search_metadata": {
        "google_flights_url": "https://www.google.com/travel/flights?tfs=abc"
    },
    "best_flights": [SAMPLE_ITEM],
    "other_flights": [
        {
            "flights": [
                {
                    "departure_airport": {"id": "BOS", "time": "2026-10-01 06:00"},
                    "arrival_airport": {"id": "IAD", "time": "2026-10-01 07:45"},
                    "airline": "United", "flight_number": "UA 512",
                },
                {
                    "departure_airport": {"id": "IAD", "time": "2026-10-01 09:00"},
                    "arrival_airport": {"id": "ORD", "time": "2026-10-01 10:05"},
                    "airline": "United", "flight_number": "UA 233",
                },
            ],
            "layovers": [{"id": "IAD", "duration": 75}],
            "total_duration": 305,
            "price": 129,
        }
    ],
}


@pytest.fixture
def provider(monkeypatch):
    p = SerpApiFlightsProvider(api_key="test")
    monkeypatch.setattr(p, "_get", lambda params: SAMPLE_RESPONSE)
    return p


@pytest.fixture
def request_round_trip():
    return TravelRequest(
        origin="BOS", destination="ORD",
        outbound_date=DateRange(start=date(2026, 10, 1)),
        return_date=DateRange(start=date(2026, 10, 5)),
    )


def test_search_maps_offers(provider, request_round_trip):
    offers = provider.search(request_round_trip)
    assert len(offers) == 2

    nonstop = offers[0]
    assert (nonstop.origin, nonstop.destination) == ("BOS", "ORD")
    assert nonstop.carrier == "UA"
    assert nonstop.connections == 0
    assert nonstop.total_price == Decimal("187")
    assert nonstop.duration_minutes == 160
    assert nonstop.booking_url.startswith("https://www.google.com/travel/flights")
    assert nonstop.provider_meta["search_params"]["return_date"] == "2026-10-05"

    onestop = offers[1]
    assert onestop.connections == 1
    assert onestop.total_price == Decimal("129")
    assert len(onestop.segments) == 2


def test_refresh_rematches_by_itinerary(provider, request_round_trip):
    offers = provider.search(request_round_trip)
    refreshed = provider.refresh(offers[0])
    assert refreshed.id == offers[0].id
    assert refreshed.itinerary_fingerprint() == offers[0].itinerary_fingerprint()


def test_refresh_missing_itinerary_raises(provider, request_round_trip, monkeypatch):
    offers = provider.search(request_round_trip)
    monkeypatch.setattr(
        provider, "_get",
        lambda params: {"best_flights": [], "other_flights": []},
    )
    with pytest.raises(OfferNotAvailableError):
        provider.refresh(offers[0])


def test_booking_operations_refuse(provider, request_round_trip):
    offers = provider.search(request_round_trip)
    with pytest.raises(ProviderNotConfiguredError):
        provider.book(offers[0], [], None)
    with pytest.raises(ProviderNotConfiguredError):
        provider.cancel("x")
    with pytest.raises(ProviderNotConfiguredError):
        provider.get_status("x")
