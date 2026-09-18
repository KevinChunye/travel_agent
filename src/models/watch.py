"""Price watches and our own observed price history."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from src.models.travel_request import TravelRequest


class PriceWatch(BaseModel):
    id: str = Field(default_factory=lambda: f"watch_{uuid.uuid4().hex[:12]}")
    user_id: str
    trip_id: Optional[str] = None
    origin: str
    destination: str
    # Full constraint snapshot so monitoring re-runs the same search.
    request: TravelRequest
    search_fingerprint: str
    target_price: Optional[float] = None
    initial_price: Optional[float] = None
    lowest_price: Optional[float] = None
    latest_price: Optional[float] = None
    currency: str = "USD"
    active: bool = True
    paused: bool = False
    target_notified: bool = False
    next_check_at: Optional[datetime] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PriceObservation(BaseModel):
    id: str = Field(default_factory=lambda: f"obs_{uuid.uuid4().hex[:12]}")
    watch_id: str
    observed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    best_price: float
    currency: str = "USD"
    airline: Optional[str] = None
    itinerary_id: Optional[str] = None  # itinerary fingerprint of best offer
    search_fingerprint: str = ""
