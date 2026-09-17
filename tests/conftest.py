from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.models.booking import PassengerIdentity, Trip, TripState
from src.models.offer import BaggageAllowance, Offer, Segment
from src.models.travel_request import DateRange, TimeWindow, TransportMode, TravelRequest
from src.providers.mock import MockFlightProvider
from src.services.booking import BookingService
from src.services.search import SearchService
from src.storage.repository import SQLiteRepository

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    r = SQLiteRepository(":memory:")
    yield r
    r.close()


@pytest.fixture
def request_bos_jfk() -> TravelRequest:
    return TravelRequest(
        origin="BOS",
        destination="JFK",
        outbound_date=DateRange(start=date(2026, 10, 9)),
        outbound_window=TimeWindow(),
    )


@pytest.fixture
def provider() -> MockFlightProvider:
    return MockFlightProvider(now=NOW)


@pytest.fixture
def trip(repo, request_bos_jfk) -> Trip:
    t = Trip(user_id="user1", request=request_bos_jfk, state=TripState.READY_TO_SEARCH)
    repo.save_trip(t)
    return t


@pytest.fixture
def search_service(repo, provider) -> SearchService:
    return SearchService([provider], repo)


@pytest.fixture
def booking_service(repo, provider) -> BookingService:
    return BookingService([provider], repo, clock=lambda: NOW)


@pytest.fixture
def traveler(repo) -> PassengerIdentity:
    t = PassengerIdentity(
        user_id="user1",
        given_name="Kevin",
        family_name="Wang",
        born_on=date(1995, 5, 1),
        email="kevin@example.com",
    )
    repo.save_traveler(t)
    return t


def make_offer(
    *,
    price: str = "150",
    duration: int = 90,
    connections: int = 0,
    dep_hour: int = 10,
    carrier: str = "DL",
    carry_on: bool = True,
    refundable: bool = False,
    changeable: bool = False,
    expires_at: datetime | None = None,
    day: date = date(2026, 10, 9),
) -> Offer:
    dep = datetime(day.year, day.month, day.day, dep_hour, 0)
    arr = dep.replace(minute=duration % 60, hour=(dep_hour + duration // 60) % 24)
    return Offer(
        provider_offer_id=f"po_{carrier}_{dep_hour}_{price}",
        mode=TransportMode.FLIGHT,
        provider="mock",
        carrier=carrier,
        origin="BOS",
        destination="JFK",
        departure=dep,
        arrival=arr,
        duration_minutes=duration,
        connections=connections,
        base_price=Decimal(price),
        fees=Decimal("0"),
        total_price=Decimal(price),
        currency="USD",
        baggage=BaggageAllowance(carry_on_included=carry_on),
        refundable=refundable,
        changeable=changeable,
        expires_at=expires_at,
        segments=[
            Segment(
                carrier=carrier,
                number=f"{carrier}{dep_hour}01",
                origin="BOS",
                destination="JFK",
                departure=dep,
                arrival=arr,
            )
        ],
    )
