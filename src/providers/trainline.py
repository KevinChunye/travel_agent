"""Trainline rail provider adapter (Stage 5 — interface stub).

Implements the same TravelProvider contract as flights so that rail
search can be added without touching agent logic. All methods currently
raise ProviderNotConfiguredError.
"""

from __future__ import annotations

from src.models.booking import PassengerIdentity, PaymentToken
from src.models.offer import Offer
from src.models.travel_request import TransportMode, TravelRequest
from src.providers.base import (
    BookingResult,
    CancellationResult,
    ProviderNotConfiguredError,
    StatusResult,
    TravelProvider,
)

_MSG = "Trainline adapter is not implemented yet (planned for Stage 5)"


class TrainlineProvider(TravelProvider):
    name = "trainline"
    modes = (TransportMode.TRAIN,)

    def search(self, request: TravelRequest) -> list[Offer]:
        raise ProviderNotConfiguredError(_MSG)

    def refresh(self, offer: Offer) -> Offer:
        raise ProviderNotConfiguredError(_MSG)

    def book(
        self,
        offer: Offer,
        passengers: list[PassengerIdentity],
        payment: PaymentToken,
    ) -> BookingResult:
        raise ProviderNotConfiguredError(_MSG)

    def cancel(self, provider_order_id: str) -> CancellationResult:
        raise ProviderNotConfiguredError(_MSG)

    def get_status(self, provider_order_id: str) -> StatusResult:
        raise ProviderNotConfiguredError(_MSG)
