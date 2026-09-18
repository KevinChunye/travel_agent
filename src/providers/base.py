"""Abstract transportation-provider interface.

Every provider (Google Flights via SerpAPI for live search, mock for
offline development) implements this interface and returns only
normalized models. Provider-specific payloads must never leak past an
adapter. The transactional methods (book/cancel) exist on the interface
for completeness; the active workflow is search-only and the live
provider raises ``ProviderNotConfiguredError`` for them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from src.models.booking import PassengerIdentity, PaymentToken
from src.models.offer import Offer
from src.models.travel_request import TransportMode, TravelRequest


class ProviderError(Exception):
    """Base class for provider failures."""


class OfferNotAvailableError(ProviderError):
    """The offer expired or was withdrawn by the provider."""


class ProviderNotConfiguredError(ProviderError):
    """Provider credentials are missing or the adapter is not implemented."""


class BookingResult(BaseModel):
    provider_order_id: str
    booking_reference: Optional[str] = None
    total_price: Decimal
    currency: str


class CancellationResult(BaseModel):
    provider_order_id: str
    cancelled: bool
    refund_amount: Optional[Decimal] = None
    currency: Optional[str] = None


class TripStatus(str, Enum):
    ON_TIME = "on_time"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    CHANGED = "changed"
    UNKNOWN = "unknown"


class StatusResult(BaseModel):
    provider_order_id: str
    status: TripStatus
    departure: Optional[datetime] = None
    arrival: Optional[datetime] = None
    origin_terminal: Optional[str] = None
    destination_terminal: Optional[str] = None
    details: Optional[str] = None

    def snapshot(self) -> dict:
        """Comparable dict for change detection in monitoring."""
        return {
            "status": self.status.value,
            "departure": self.departure.isoformat() if self.departure else None,
            "arrival": self.arrival.isoformat() if self.arrival else None,
            "origin_terminal": self.origin_terminal,
            "destination_terminal": self.destination_terminal,
        }


class TravelProvider(ABC):
    """Contract for all transportation providers."""

    #: Unique provider name, e.g. "google_flights", "mock".
    name: str = "abstract"
    #: Transportation modes this provider can search.
    modes: tuple[TransportMode, ...] = ()

    def supports(self, mode: TransportMode) -> bool:
        return mode in self.modes

    @abstractmethod
    def search(self, request: TravelRequest) -> list[Offer]:
        """Search itineraries matching the request. Returns normalized offers."""

    @abstractmethod
    def refresh(self, offer: Offer) -> Offer:
        """Re-fetch an offer to get current price/availability.

        Must return a *new* Offer (fresh provider_offer_id if the provider
        reissues them) or raise OfferNotAvailableError.
        """

    @abstractmethod
    def book(
        self,
        offer: Offer,
        passengers: list[PassengerIdentity],
        payment: PaymentToken,
    ) -> BookingResult:
        """Purchase the offer. Only the BookingService may call this."""

    @abstractmethod
    def cancel(self, provider_order_id: str) -> CancellationResult:
        """Cancel an existing order."""

    @abstractmethod
    def get_status(self, provider_order_id: str) -> StatusResult:
        """Current status of a booked order (for monitoring)."""
