"""Confirmed trips, created after the user books externally.

The agent never handles payment. When the user says "booked", a
BookedTrip is created from the selected offer plus whatever details the
user supplies (confirmation code, corrected times). Designed so Gmail
confirmation-email ingestion can later populate the same model.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class BookedTripStatus(str, Enum):
    CONFIRMED = "confirmed"
    CHANGED = "changed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class BookedTrip(BaseModel):
    id: str = Field(default_factory=lambda: f"btrip_{uuid.uuid4().hex[:12]}")
    user_id: str
    trip_id: Optional[str] = None  # originating search/conversation trip
    airline: Optional[str] = None
    flight_number: Optional[str] = None
    confirmation_code: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure: Optional[datetime] = None
    arrival: Optional[datetime] = None
    booking_url: Optional[str] = None
    price: Optional[str] = None  # informational only; user-reported
    currency: str = "USD"
    status: BookedTripStatus = BookedTripStatus.CONFIRMED
    source: str = "manual"  # manual | gmail (future) | import (future)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
