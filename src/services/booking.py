"""Protected booking flow.

Authorization is enforced here, in deterministic code, independent of
the LLM. The model can only call these functions; it cannot alter the
checks. Every paid booking requires a persisted, explicitly confirmed
BookingIntent, and the offer is re-validated against that intent at
execution time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from src.models.booking import (
    Booking,
    BookingIntent,
    PassengerIdentity,
    PaymentToken,
    Trip,
    TripState,
)
from src.models.offer import Offer
from src.providers.base import OfferNotAvailableError, ProviderError, TravelProvider
from src.services import state as sm
from src.storage.repository import Repository

logger = logging.getLogger(__name__)

#: A confirmation is only valid for this long before we ask again.
INTENT_MAX_AGE = timedelta(minutes=15)
#: Repricing tolerance: any increase beyond the approved max is rejected.


class BookingAuthorizationError(Exception):
    """Raised when a booking attempt fails a hard authorization rule."""


class RejectionReason(str, Enum):
    NOT_CONFIRMED = "not_confirmed"
    INTENT_CONSUMED = "intent_consumed"
    INTENT_STALE = "intent_stale"
    PRICE_ABOVE_APPROVED = "price_above_approved"
    ITINERARY_CHANGED = "itinerary_changed"
    OFFER_CHANGED = "offer_changed"
    PASSENGER_CHANGED = "passenger_changed"
    BAGGAGE_CHANGED = "baggage_changed"
    OFFER_EXPIRED = "offer_expired"
    PROVIDER_ERROR = "provider_error"
    PAYMENT_FAILED = "payment_failed"


class BookingDecision(BaseModel):
    booked: bool
    booking_id: Optional[str] = None
    provider_order_id: Optional[str] = None
    booking_reference: Optional[str] = None
    rejection: Optional[RejectionReason] = None
    requires_reconfirmation: bool = False
    detail: Optional[str] = None
    current_offer: Optional[Offer] = None  # what to re-show the user


class BookingService:
    def __init__(
        self,
        providers: list[TravelProvider],
        repo: Repository,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._providers = {p.name: p for p in providers}
        self._repo = repo
        self._clock = clock

    def _provider_for(self, offer: Offer) -> TravelProvider:
        provider = self._providers.get(offer.provider)
        if provider is None:
            raise BookingAuthorizationError(
                f"No provider registered for {offer.provider!r}"
            )
        return provider

    # -- step 1: refresh the selected offer and build an intent --------------

    def reprice_and_create_intent(
        self,
        trip: Trip,
        passenger: PassengerIdentity,
        price_buffer_pct: float = 0.0,
    ) -> tuple[BookingIntent, Offer]:
        """Refresh the selected offer and create an *unconfirmed* intent.

        The caller must then display the refreshed itinerary, restrictions,
        passenger and exact total price, and only mark the intent confirmed
        after the user explicitly approves that display.
        """
        if trip.selected_offer_id is None:
            raise BookingAuthorizationError("No offer selected for this trip")
        offer = self._repo.get_offer(trip.selected_offer_id)
        if offer is None:
            raise BookingAuthorizationError("Selected offer not found in storage")

        sm.transition(self._repo, trip, TripState.REPRICING)
        provider = self._provider_for(offer)
        try:
            refreshed = provider.refresh(offer)
        except OfferNotAvailableError:
            sm.transition(self._repo, trip, TripState.OFFER_EXPIRED)
            raise
        refreshed.id = offer.id  # keep internal identity
        self._repo.save_offers(trip.id, [refreshed])

        intent = BookingIntent.from_offer(
            trip_id=trip.id,
            user_id=trip.user_id,
            selected_offer_id=offer.id,
            refreshed_offer=refreshed,
            passenger_identity_hash=passenger.identity_hash(),
            price_buffer_pct=price_buffer_pct,
        )
        self._repo.save_intent(intent)
        trip.active_intent_id = intent.id
        sm.transition(self._repo, trip, TripState.AWAITING_BOOKING_CONFIRMATION)
        return intent, refreshed

    # -- step 2: record the user's explicit confirmation ----------------------

    def confirm_intent(self, intent_id: str, user_id: str) -> BookingIntent:
        """Mark an intent confirmed. Call this only from the tool layer after
        the user has explicitly said yes to the displayed price/itinerary."""
        intent = self._repo.get_intent(intent_id)
        if intent is None:
            raise BookingAuthorizationError(f"Unknown intent {intent_id}")
        if intent.user_id != user_id:
            raise BookingAuthorizationError("Intent belongs to a different user")
        if intent.consumed:
            raise BookingAuthorizationError("Intent already used")
        intent.user_confirmed = True
        intent.confirmed_at = self._clock()
        self._repo.save_intent(intent)
        return intent

    # -- step 3: execute -------------------------------------------------------

    def execute_booking(
        self,
        trip: Trip,
        intent_id: str,
        passenger: PassengerIdentity,
        payment: PaymentToken,
    ) -> BookingDecision:
        """Book only if every authorization rule passes.

        The intent is re-read from storage (never trusted from the caller),
        and the offer is refreshed one final time and compared against what
        the user approved.
        """
        intent = self._repo.get_intent(intent_id)
        if intent is None or intent.trip_id != trip.id:
            raise BookingAuthorizationError("Intent not found for this trip")

        def reject(
            reason: RejectionReason,
            detail: str,
            *,
            reconfirm: bool = False,
            new_state: Optional[TripState] = None,
            current_offer: Optional[Offer] = None,
        ) -> BookingDecision:
            logger.info("booking rejected (%s): %s", reason.value, detail)
            if new_state is not None and trip.state != new_state:
                sm.transition(self._repo, trip, new_state)
            return BookingDecision(
                booked=False,
                rejection=reason,
                requires_reconfirmation=reconfirm,
                detail=detail,
                current_offer=current_offer,
            )

        # Rule 1: explicit confirmation must be present.
        if not intent.user_confirmed or intent.confirmed_at is None:
            return reject(
                RejectionReason.NOT_CONFIRMED,
                "User has not explicitly confirmed this booking",
            )
        if intent.consumed:
            return reject(
                RejectionReason.INTENT_CONSUMED,
                "This confirmation was already used",
            )
        # Rule 2: confirmations expire.
        if self._clock() - intent.confirmed_at > INTENT_MAX_AGE:
            return reject(
                RejectionReason.INTENT_STALE,
                "Confirmation is too old; please review and confirm again",
                reconfirm=True,
            )
        # Rule 3: passenger identity must match what was approved.
        if passenger.identity_hash() != intent.passenger_identity_hash:
            return reject(
                RejectionReason.PASSENGER_CHANGED,
                "Passenger details differ from the confirmed booking",
                reconfirm=True,
            )

        offer = self._repo.get_offer(intent.selected_offer_id)
        if offer is None:
            return reject(
                RejectionReason.OFFER_CHANGED, "Selected offer no longer in storage"
            )
        if offer.provider_offer_id != intent.refreshed_provider_offer_id:
            return reject(
                RejectionReason.OFFER_CHANGED,
                "Selected offer changed since confirmation",
                reconfirm=True,
            )

        # Final live refresh before paying.
        provider = self._provider_for(offer)
        try:
            live = provider.refresh(offer)
        except OfferNotAvailableError as exc:
            return reject(
                RejectionReason.OFFER_EXPIRED,
                str(exc),
                reconfirm=True,
                new_state=TripState.OFFER_EXPIRED,
            )
        except ProviderError as exc:
            return reject(RejectionReason.PROVIDER_ERROR, str(exc))
        live.id = offer.id
        self._repo.save_offers(trip.id, [live])

        # Rule 4: price must not exceed the approved maximum.
        if live.total_price > intent.max_approved_total_price:
            return reject(
                RejectionReason.PRICE_ABOVE_APPROVED,
                f"Price is now {live.total_price} {live.currency}, above the "
                f"approved {intent.max_approved_total_price} {intent.currency}",
                reconfirm=True,
                new_state=TripState.PRICE_CHANGED,
                current_offer=live,
            )
        # Rule 5: the itinerary must be materially unchanged.
        if live.itinerary_fingerprint() != intent.expected_itinerary_fingerprint:
            return reject(
                RejectionReason.ITINERARY_CHANGED,
                "The itinerary changed since you confirmed",
                reconfirm=True,
                new_state=TripState.PRICE_CHANGED,
                current_offer=live,
            )
        # Rule 6: baggage allowance must be what was approved.
        if live.baggage_fingerprint() != intent.expected_baggage_fingerprint:
            return reject(
                RejectionReason.BAGGAGE_CHANGED,
                "Included baggage changed since you confirmed",
                reconfirm=True,
                new_state=TripState.PRICE_CHANGED,
                current_offer=live,
            )
        if live.is_expired(self._clock()):
            return reject(
                RejectionReason.OFFER_EXPIRED,
                "Offer expired before booking",
                reconfirm=True,
                new_state=TripState.OFFER_EXPIRED,
            )

        # All rules passed: consume the intent and book.
        intent.consumed = True
        self._repo.save_intent(intent)
        sm.transition(self._repo, trip, TripState.BOOKING)
        try:
            result = provider.book(live, [passenger], payment)
        except OfferNotAvailableError as exc:
            sm.transition(self._repo, trip, TripState.OFFER_EXPIRED)
            return BookingDecision(
                booked=False,
                rejection=RejectionReason.OFFER_EXPIRED,
                requires_reconfirmation=True,
                detail=str(exc),
            )
        except ProviderError as exc:
            sm.transition(self._repo, trip, TripState.BOOKING_FAILED)
            return BookingDecision(
                booked=False,
                rejection=RejectionReason.PROVIDER_ERROR,
                detail=str(exc),
            )

        booking = Booking(
            trip_id=trip.id,
            user_id=trip.user_id,
            provider=provider.name,
            provider_order_id=result.provider_order_id,
            booking_reference=result.booking_reference,
            offer=live,
            passenger_identity_hash=intent.passenger_identity_hash,
            total_paid=result.total_price,
            currency=result.currency,
        )
        self._repo.save_booking(booking)
        trip.booking_id = booking.id
        sm.transition(self._repo, trip, TripState.CONFIRMED)
        return BookingDecision(
            booked=True,
            booking_id=booking.id,
            provider_order_id=result.provider_order_id,
            booking_reference=result.booking_reference,
        )


def format_confirmation_prompt(
    intent: BookingIntent, offer: Offer, passenger: PassengerIdentity
) -> str:
    """Exact display shown to the user before they confirm. The channel
    layer sends this verbatim; the LLM must not paraphrase prices."""
    dur_h, dur_m = divmod(offer.duration_minutes, 60)
    bags = []
    if offer.baggage.carry_on_included:
        bags.append("1 carry-on included")
    if offer.baggage.checked_bags_included:
        bags.append(f"{offer.baggage.checked_bags_included} checked bag(s) included")
    lines = [
        "Please review before booking:",
        f"  {offer.carrier_name or offer.carrier} "
        f"({offer.mode.value}), {offer.origin} → {offer.destination}",
        f"  Depart {offer.departure:%a %b %d, %H:%M} · "
        f"arrive {offer.arrival:%a %b %d, %H:%M} ({dur_h}h{dur_m:02d}, "
        f"{'nonstop' if offer.connections == 0 else str(offer.connections) + ' stop(s)'})",
        f"  Passenger: {passenger.given_name} {passenger.family_name}",
        f"  Baggage: {'; '.join(bags) or 'no bags included'}",
        f"  Fare rules: "
        f"{'refundable' if offer.refundable else 'non-refundable'}, "
        f"{'changes allowed' if offer.changeable else 'no changes'}",
    ]
    if offer.restrictions:
        lines.append(f"  Restrictions: {'; '.join(offer.restrictions)}")
    lines += [
        f"  TOTAL: {offer.total_price} {offer.currency}",
        f"Reply YES to book at exactly this price (valid "
        f"{int(INTENT_MAX_AGE.total_seconds() // 60)} minutes), or NO to cancel.",
    ]
    return "\n".join(lines)
