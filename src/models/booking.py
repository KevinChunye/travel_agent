"""Trip lifecycle, booking authorization, and booking records.

The state machine and the BookingIntent are the security core of the
system: bookings are authorized by persisted data and deterministic
checks, never by conversation history.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.models.offer import Offer
from src.models.travel_request import TravelRequest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TripState(str, Enum):
    DRAFT = "DRAFT"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    READY_TO_SEARCH = "READY_TO_SEARCH"
    SEARCHING = "SEARCHING"
    OPTIONS_READY = "OPTIONS_READY"
    OPTION_SELECTED = "OPTION_SELECTED"
    # Active (link-handoff) flow: the agent never takes payment.
    BOOKING_LINK_READY = "BOOKING_LINK_READY"
    AWAITING_USER_BOOKING = "AWAITING_USER_BOOKING"
    TRIP_CONFIRMED = "TRIP_CONFIRMED"
    MONITORING = "MONITORING"
    COMPLETED = "COMPLETED"
    # Price tracking
    PRICE_WATCH_ACTIVE = "PRICE_WATCH_ACTIVE"
    TRACKING_PAUSED = "TRACKING_PAUSED"
    # Search outcomes
    NO_RESULTS = "NO_RESULTS"
    SEARCH_FAILED = "SEARCH_FAILED"
    SEARCH_QUOTA_REACHED = "SEARCH_QUOTA_REACHED"
    BOOKING_LINK_UNAVAILABLE = "BOOKING_LINK_UNAVAILABLE"
    # Exceptional states
    OFFER_EXPIRED = "OFFER_EXPIRED"
    PRICE_CHANGED = "PRICE_CHANGED"
    TRIP_CHANGED = "TRIP_CHANGED"
    CANCELLED = "CANCELLED"
    # LEGACY transactional states: kept only so historical trip rows
    # remain readable; NOT reachable from the active workflow (the agent
    # never performs financial transactions — test-asserted).
    REPRICING = "REPRICING"
    AWAITING_BOOKING_CONFIRMATION = "AWAITING_BOOKING_CONFIRMATION"
    BOOKING = "BOOKING"
    CONFIRMED = "CONFIRMED"
    BOOKING_FAILED = "BOOKING_FAILED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class Trip(BaseModel):
    """One travel intent from first message to completion."""

    id: str = Field(default_factory=lambda: f"trip_{uuid.uuid4().hex[:12]}")
    user_id: str
    request: TravelRequest
    state: TripState = TripState.DRAFT
    selected_offer_id: Optional[str] = None
    active_intent_id: Optional[str] = None
    booking_id: Optional[str] = None
    presented_offer_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PassengerIdentity(BaseModel):
    """PII for a traveler. Stored in its own table, never in chat memory.

    Payment data is intentionally absent: we never hold card numbers or
    CVVs — only provider-side payment tokens (see PaymentToken).
    """

    id: str = Field(default_factory=lambda: f"pax_{uuid.uuid4().hex[:12]}")
    user_id: str
    given_name: str
    family_name: str
    born_on: Optional[date] = None
    gender: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    passport_number_last4: Optional[str] = None

    def identity_hash(self) -> str:
        raw = "|".join(
            [
                self.given_name.strip().lower(),
                self.family_name.strip().lower(),
                self.born_on.isoformat() if self.born_on else "",
                (self.email or "").strip().lower(),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()


class PaymentToken(BaseModel):
    """Reference to a provider-side payment method. Never a card number."""

    provider: str
    token: str  # provider-side token id, never a card number
    kind: str = "balance"


class BookingIntent(BaseModel):
    """The authorization record for a paid booking.

    Created when the user asks to book, confirmed only after the user has
    seen the exact refreshed itinerary and price and explicitly approved.
    The booking service re-validates everything at execution time.
    """

    id: str = Field(default_factory=lambda: f"intent_{uuid.uuid4().hex[:12]}")
    trip_id: str
    user_id: str
    selected_offer_id: str  # internal offer id the user picked
    refreshed_provider_offer_id: str  # provider offer id after repricing
    expected_itinerary_fingerprint: str
    expected_baggage_fingerprint: str
    expected_departure: datetime
    expected_arrival: datetime
    expected_carrier: str
    passenger_identity_hash: str
    approved_total_price: Decimal
    max_approved_total_price: Decimal
    currency: str
    created_at: datetime = Field(default_factory=_utcnow)
    user_confirmed: bool = False
    confirmed_at: Optional[datetime] = None
    consumed: bool = False  # set once a booking attempt has used this intent

    @classmethod
    def from_offer(
        cls,
        *,
        trip_id: str,
        user_id: str,
        selected_offer_id: str,
        refreshed_offer: Offer,
        passenger_identity_hash: str,
        price_buffer_pct: float = 0.0,
    ) -> "BookingIntent":
        approved = refreshed_offer.total_price
        max_approved = (
            approved * (Decimal("1") + Decimal(str(price_buffer_pct)) / 100)
        ).quantize(Decimal("0.01"))
        return cls(
            trip_id=trip_id,
            user_id=user_id,
            selected_offer_id=selected_offer_id,
            refreshed_provider_offer_id=refreshed_offer.provider_offer_id,
            expected_itinerary_fingerprint=refreshed_offer.itinerary_fingerprint(),
            expected_baggage_fingerprint=refreshed_offer.baggage_fingerprint(),
            expected_departure=refreshed_offer.departure,
            expected_arrival=refreshed_offer.arrival,
            expected_carrier=refreshed_offer.carrier,
            passenger_identity_hash=passenger_identity_hash,
            approved_total_price=approved,
            max_approved_total_price=max_approved,
            currency=refreshed_offer.currency,
        )


class BookingStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    CHANGED = "changed"
    UNKNOWN = "unknown"


class Booking(BaseModel):
    """A completed (paid) booking."""

    id: str = Field(default_factory=lambda: f"bkg_{uuid.uuid4().hex[:12]}")
    trip_id: str
    user_id: str
    provider: str
    provider_order_id: str
    booking_reference: Optional[str] = None
    offer: Offer  # snapshot of what was booked
    passenger_identity_hash: str
    total_paid: Decimal
    currency: str
    status: BookingStatus = BookingStatus.CONFIRMED
    booked_at: datetime = Field(default_factory=_utcnow)
