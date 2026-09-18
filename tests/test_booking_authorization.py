"""Booking-authorization rules: the model must not be able to buy anything
the user did not explicitly approve, at a price they did not approve."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.models.booking import PassengerIdentity, PaymentToken, TripState
from src.providers.base import OfferNotAvailableError
from src.services.booking import BookingService, RejectionReason

PAYMENT = PaymentToken(provider="mock", token="balance", kind="balance")


def drive_to_intent(repo, search_service, booking_service, trip, traveler):
    """Search, pick option 1, reprice -> unconfirmed intent."""
    search_service.search(trip)
    search_service.select_option(trip, "1")
    intent, refreshed = booking_service.reprice_and_create_intent(trip, traveler)
    return intent, refreshed


def test_booking_rejected_without_confirmation(
    repo, search_service, booking_service, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.NOT_CONFIRMED
    assert repo.get_trip(trip.id).booking_id is None


def test_confirmed_booking_succeeds_and_persists(
    repo, search_service, booking_service, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    booking_service.confirm_intent(intent.id, trip.user_id)
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert decision.booked
    assert trip.state == TripState.CONFIRMED
    booking = repo.get_booking(decision.booking_id)
    assert booking is not None
    assert booking.passenger_identity_hash == traveler.identity_hash()


def test_price_increase_beyond_approved_requires_reconfirmation(
    repo, search_service, booking_service, provider, trip, traveler
):
    intent, refreshed = drive_to_intent(
        repo, search_service, booking_service, trip, traveler
    )
    booking_service.confirm_intent(intent.id, trip.user_id)
    provider.price_bump_on_refresh = Decimal("50")  # price rises after approval
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.PRICE_ABOVE_APPROVED
    assert decision.requires_reconfirmation
    assert repo.get_trip(trip.id).state == TripState.PRICE_CHANGED
    # The new live price is surfaced so the user can re-approve it.
    assert decision.current_offer.total_price == refreshed.total_price + 50


def test_material_itinerary_change_blocks_booking(
    repo, search_service, booking_service, provider, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    booking_service.confirm_intent(intent.id, trip.user_id)
    provider.change_itinerary_on_refresh = True
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.ITINERARY_CHANGED
    assert decision.requires_reconfirmation


def test_offer_expiring_blocks_booking(
    repo, search_service, booking_service, provider, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    booking_service.confirm_intent(intent.id, trip.user_id)
    provider.expire_on_refresh = True
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.OFFER_EXPIRED
    assert repo.get_trip(trip.id).state == TripState.OFFER_EXPIRED


def test_passenger_change_blocks_booking(
    repo, search_service, booking_service, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    booking_service.confirm_intent(intent.id, trip.user_id)
    other = PassengerIdentity(
        user_id="user1", given_name="Someone", family_name="Else",
        born_on=date(1990, 1, 1),
    )
    decision = booking_service.execute_booking(trip, intent.id, other, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.PASSENGER_CHANGED


def test_stale_confirmation_rejected(
    repo, search_service, provider, trip, traveler
):
    from tests.conftest import NOW

    current = {"now": NOW}
    service = BookingService([provider], repo, clock=lambda: current["now"])
    intent, _ = drive_to_intent(repo, search_service, service, trip, traveler)
    service.confirm_intent(intent.id, trip.user_id)
    current["now"] = NOW + timedelta(minutes=20)  # past INTENT_MAX_AGE
    decision = service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.INTENT_STALE
    assert decision.requires_reconfirmation


def test_intent_cannot_be_reused(
    repo, search_service, booking_service, trip, traveler
):
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    booking_service.confirm_intent(intent.id, trip.user_id)
    first = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert first.booked
    second = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not second.booked
    assert second.rejection == RejectionReason.INTENT_CONSUMED


def test_confirm_rejects_wrong_user(
    repo, search_service, booking_service, trip, traveler
):
    from src.services.booking import BookingAuthorizationError

    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    with pytest.raises(BookingAuthorizationError):
        booking_service.confirm_intent(intent.id, "someone_else")


def test_reprice_on_withdrawn_offer_moves_to_expired(
    repo, search_service, booking_service, provider, trip, traveler
):
    search_service.search(trip)
    search_service.select_option(trip, "1")
    provider.expire_on_refresh = True
    with pytest.raises(OfferNotAvailableError):
        booking_service.reprice_and_create_intent(trip, traveler)
    assert repo.get_trip(trip.id).state == TripState.OFFER_EXPIRED


def test_confirmation_flag_alone_is_not_enough(
    repo, search_service, booking_service, trip, traveler
):
    """Even if a confused/prompt-injected model hands over an intent object
    claiming confirmation, execution re-reads the persisted intent."""
    intent, _ = drive_to_intent(repo, search_service, booking_service, trip, traveler)
    intent.user_confirmed = True  # tampered in memory, never persisted via confirm
    decision = booking_service.execute_booking(trip, intent.id, traveler, PAYMENT)
    assert not decision.booked
    assert decision.rejection == RejectionReason.NOT_CONFIRMED
