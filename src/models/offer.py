"""Normalized offer model.

Every provider adapter (flights, trains, buses) must map its results into
this representation before anything else in the system sees them.
Provider-specific payloads never leave the adapter layer.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from src.models.travel_request import CabinClass, TransportMode


def _new_offer_id() -> str:
    return f"off_{uuid.uuid4().hex[:16]}"


class Segment(BaseModel):
    """One leg of an itinerary (a single flight / train / bus ride)."""

    carrier: str
    carrier_name: Optional[str] = None
    number: Optional[str] = None
    origin: str
    destination: str
    departure: datetime
    arrival: datetime


class BaggageAllowance(BaseModel):
    carry_on_included: bool = False
    checked_bags_included: int = 0
    notes: Optional[str] = None


class Offer(BaseModel):
    """A single bookable itinerary, normalized across all providers."""

    id: str = Field(default_factory=_new_offer_id)
    provider_offer_id: str
    mode: TransportMode
    provider: str
    carrier: str
    carrier_name: Optional[str] = None
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    duration_minutes: int
    door_to_door_minutes: Optional[int] = None
    connections: int = 0
    self_transfer: bool = False
    base_price: Decimal
    fees: Decimal = Decimal("0")
    total_price: Decimal
    currency: str = "USD"
    cabin: Optional[CabinClass] = None
    baggage: BaggageAllowance = Field(default_factory=BaggageAllowance)
    refundable: Optional[bool] = None
    changeable: Optional[bool] = None
    expires_at: Optional[datetime] = None
    restrictions: list[str] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        elif expires.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= expires

    def is_red_eye(self) -> bool:
        """Departure in the late-night band (local time)."""
        return self.departure.hour >= 22 or self.departure.hour < 5

    def effective_travel_minutes(self) -> int:
        return self.door_to_door_minutes or self.duration_minutes

    def itinerary_fingerprint(self) -> str:
        """Stable hash of what the traveler actually experiences.

        Used to detect material itinerary changes between offer selection
        and booking. Price is intentionally excluded (checked separately);
        offer IDs are excluded (a re-fetched identical itinerary should
        match).
        """
        parts = [self.mode.value, self.carrier, self.origin, self.destination]
        for seg in self.segments or [
            Segment(
                carrier=self.carrier,
                origin=self.origin,
                destination=self.destination,
                departure=self.departure,
                arrival=self.arrival,
            )
        ]:
            parts.append(
                f"{seg.carrier}|{seg.number or ''}|{seg.origin}|{seg.destination}"
                f"|{seg.departure.isoformat()}|{seg.arrival.isoformat()}"
            )
        parts.append(f"cabin={self.cabin.value if self.cabin else ''}")
        return hashlib.sha256("~".join(parts).encode()).hexdigest()

    def baggage_fingerprint(self) -> str:
        return (
            f"carry_on={self.baggage.carry_on_included}"
            f"|checked={self.baggage.checked_bags_included}"
        )

    def is_essentially_same_as(self, other: "Offer") -> bool:
        """True when two offers would look like duplicates to the user."""
        if self.itinerary_fingerprint() == other.itinerary_fingerprint():
            return True
        same_route = (
            self.carrier == other.carrier
            and self.connections == other.connections
            and abs((self.departure - other.departure).total_seconds()) <= 15 * 60
            and abs((self.arrival - other.arrival).total_seconds()) <= 15 * 60
        )
        if not same_route:
            return False
        hi = max(self.total_price, other.total_price)
        lo = min(self.total_price, other.total_price)
        return hi == 0 or (hi - lo) / hi <= Decimal("0.05")
