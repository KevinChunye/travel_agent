"""Deterministic mock provider for Stage 1 development and tests.

Generates a stable, realistic set of flight offers from a seed derived
from the route and date, so conversations and tests are reproducible.
Test knobs allow simulating price changes, expired offers, itinerary
changes, and cancellations on refresh/status calls.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from src.models.booking import PassengerIdentity, PaymentToken
from src.models.offer import BaggageAllowance, Offer, Segment
from src.models.travel_request import TransportMode, TravelRequest
from src.providers.base import (
    BookingResult,
    CancellationResult,
    OfferNotAvailableError,
    StatusResult,
    TravelProvider,
    TripStatus,
)

_CARRIERS = [
    ("DL", "Delta Air Lines"),
    ("AA", "American Airlines"),
    ("UA", "United Airlines"),
    ("B6", "JetBlue"),
    ("NK", "Spirit Airlines"),
]

_HUBS = ["ORD", "ATL", "CLT", "PHL", "IAD"]


class MockFlightProvider(TravelProvider):
    name = "mock"
    modes = (TransportMode.FLIGHT,)

    def __init__(
        self,
        *,
        offer_ttl_minutes: int = 30,
        price_bump_on_refresh: Decimal = Decimal("0"),
        expire_on_refresh: bool = False,
        change_itinerary_on_refresh: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        self.offer_ttl_minutes = offer_ttl_minutes
        self.price_bump_on_refresh = price_bump_on_refresh
        self.expire_on_refresh = expire_on_refresh
        self.change_itinerary_on_refresh = change_itinerary_on_refresh
        self._now = now
        self._orders: dict[str, Offer] = {}
        self._order_status: dict[str, StatusResult] = {}

    def _clock(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def search(self, request: TravelRequest) -> list[Offer]:
        assert request.origin and request.destination and request.outbound_date
        day = request.outbound_date.start
        seed = hashlib.sha256(
            f"{request.origin}|{request.destination}|{day.isoformat()}".encode()
        ).hexdigest()
        rng = random.Random(seed)
        offers: list[Offer] = []
        expires_at = self._clock() + timedelta(minutes=self.offer_ttl_minutes)

        # Nonstops spread across the day, including one red-eye.
        for hour in (6, 9, 13, 16, 18, 21, 23):
            carrier, carrier_name = _CARRIERS[rng.randrange(len(_CARRIERS))]
            dep = datetime(day.year, day.month, day.day, hour, rng.choice((0, 15, 30, 45)))
            duration = rng.randrange(65, 95)
            arr = dep + timedelta(minutes=duration)
            base = Decimal(rng.randrange(90, 320))
            fees = Decimal(rng.randrange(15, 45))
            budget = carrier == "NK"
            offers.append(
                Offer(
                    provider_offer_id=f"mock_{seed[:6]}_{hour}",
                    mode=TransportMode.FLIGHT,
                    provider=self.name,
                    carrier=carrier,
                    carrier_name=carrier_name,
                    origin=request.origin,
                    destination=request.destination,
                    departure=dep,
                    arrival=arr,
                    duration_minutes=duration,
                    door_to_door_minutes=duration + 150,
                    connections=0,
                    base_price=base,
                    fees=fees,
                    total_price=base + fees,
                    currency="USD",
                    cabin=request.cabin,
                    baggage=BaggageAllowance(
                        carry_on_included=not budget,
                        checked_bags_included=0,
                    ),
                    refundable=rng.random() > 0.7,
                    changeable=rng.random() > 0.4,
                    expires_at=expires_at,
                    restrictions=["Basic fare: no carry-on included"] if budget else [],
                    segments=[
                        Segment(
                            carrier=carrier,
                            carrier_name=carrier_name,
                            number=f"{carrier}{rng.randrange(100, 999)}",
                            origin=request.origin,
                            destination=request.destination,
                            departure=dep,
                            arrival=arr,
                        )
                    ],
                )
            )

        # A couple of cheaper one-stop options through a hub.
        for i in range(2):
            carrier, carrier_name = _CARRIERS[rng.randrange(len(_CARRIERS))]
            hub = _HUBS[rng.randrange(len(_HUBS))]
            dep = datetime(day.year, day.month, day.day, rng.choice((7, 11, 15)), 30)
            leg1 = rng.randrange(90, 150)
            layover = rng.randrange(50, 120)
            leg2 = rng.randrange(90, 150)
            mid_arr = dep + timedelta(minutes=leg1)
            mid_dep = mid_arr + timedelta(minutes=layover)
            arr = mid_dep + timedelta(minutes=leg2)
            total = leg1 + layover + leg2
            base = Decimal(rng.randrange(60, 140))
            offers.append(
                Offer(
                    provider_offer_id=f"mock_{seed[:6]}_c{i}",
                    mode=TransportMode.FLIGHT,
                    provider=self.name,
                    carrier=carrier,
                    carrier_name=carrier_name,
                    origin=request.origin,
                    destination=request.destination,
                    departure=dep,
                    arrival=arr,
                    duration_minutes=total,
                    door_to_door_minutes=total + 150,
                    connections=1,
                    base_price=base,
                    fees=Decimal("22"),
                    total_price=base + Decimal("22"),
                    currency="USD",
                    cabin=request.cabin,
                    baggage=BaggageAllowance(carry_on_included=True),
                    refundable=False,
                    changeable=rng.random() > 0.5,
                    expires_at=expires_at,
                    segments=[
                        Segment(
                            carrier=carrier, carrier_name=carrier_name,
                            number=f"{carrier}{rng.randrange(100, 999)}",
                            origin=request.origin, destination=hub,
                            departure=dep, arrival=mid_arr,
                        ),
                        Segment(
                            carrier=carrier, carrier_name=carrier_name,
                            number=f"{carrier}{rng.randrange(100, 999)}",
                            origin=hub, destination=request.destination,
                            departure=mid_dep, arrival=arr,
                        ),
                    ],
                )
            )
        return offers

    def refresh(self, offer: Offer) -> Offer:
        if self.expire_on_refresh:
            raise OfferNotAvailableError(
                f"Offer {offer.provider_offer_id} is no longer available"
            )
        refreshed = offer.model_copy(deep=True)
        refreshed.provider_offer_id = f"{offer.provider_offer_id}_r"
        refreshed.expires_at = self._clock() + timedelta(
            minutes=self.offer_ttl_minutes
        )
        if self.price_bump_on_refresh:
            refreshed.base_price += self.price_bump_on_refresh
            refreshed.total_price += self.price_bump_on_refresh
        if self.change_itinerary_on_refresh:
            shift = timedelta(hours=2)
            refreshed.departure += shift
            refreshed.arrival += shift
            for seg in refreshed.segments:
                seg.departure += shift
                seg.arrival += shift
        return refreshed

    def book(
        self,
        offer: Offer,
        passengers: list[PassengerIdentity],
        payment: PaymentToken,
    ) -> BookingResult:
        if offer.is_expired(self._clock()):
            raise OfferNotAvailableError(f"Offer {offer.provider_offer_id} expired")
        order_id = f"ord_{hashlib.sha256(offer.provider_offer_id.encode()).hexdigest()[:10]}"
        self._orders[order_id] = offer
        self._order_status[order_id] = StatusResult(
            provider_order_id=order_id,
            status=TripStatus.ON_TIME,
            departure=offer.departure,
            arrival=offer.arrival,
        )
        return BookingResult(
            provider_order_id=order_id,
            booking_reference=f"PNR{order_id[-6:].upper()}",
            total_price=offer.total_price,
            currency=offer.currency,
        )

    def cancel(self, provider_order_id: str) -> CancellationResult:
        offer = self._orders.get(provider_order_id)
        self._order_status[provider_order_id] = StatusResult(
            provider_order_id=provider_order_id, status=TripStatus.CANCELLED
        )
        return CancellationResult(
            provider_order_id=provider_order_id,
            cancelled=True,
            refund_amount=offer.total_price if offer and offer.refundable else Decimal("0"),
            currency=offer.currency if offer else None,
        )

    def get_status(self, provider_order_id: str) -> StatusResult:
        return self._order_status.get(
            provider_order_id,
            StatusResult(provider_order_id=provider_order_id, status=TripStatus.UNKNOWN),
        )

    # Test helper: simulate an external schedule change on a booked order.
    def simulate_status(self, provider_order_id: str, status: StatusResult) -> None:
        self._order_status[provider_order_id] = status
