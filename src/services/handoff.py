"""Booking-link handoff: the active alternative to in-agent booking.

The agent never takes payment and never collects card or passport data.
When the user picks an option, we hand them the Google Flights /
airline URL; they purchase externally, then tell the agent "booked",
which creates a confirmed BookedTrip the agent can manage and monitor.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from src.models.booking import Trip, TripState
from src.models.offer import Offer
from src.models.trip import BookedTrip
from src.services import state as sm
from src.storage.repository import Repository

PAYMENT_DISCLAIMER = (
    "Complete payment on Google Flights / the airline's own website. "
    "I never ask for or store card numbers, CVVs, or passport details — "
    "never share them with me."
)


class HandoffResult(BaseModel):
    trip_id: str
    state: str
    booking_url: Optional[str] = None
    display_text: str


class HandoffService:
    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def booking_link(self, trip: Trip) -> HandoffResult:
        """OPTION_SELECTED -> BOOKING_LINK_READY -> AWAITING_USER_BOOKING,
        or BOOKING_LINK_UNAVAILABLE when the offer carries no URL."""
        if trip.selected_offer_id is None:
            raise ValueError("No offer selected for this trip")
        offer = self._repo.get_offer(trip.selected_offer_id)
        if offer is None:
            raise ValueError("Selected offer not found in storage")

        if not offer.booking_url:
            sm.transition(self._repo, trip, TripState.BOOKING_LINK_UNAVAILABLE)
            return HandoffResult(
                trip_id=trip.id,
                state=trip.state.value,
                display_text=(
                    "No booking link is available for this result. Search the "
                    f"itinerary ({offer.carrier_name or offer.carrier} "
                    f"{offer.origin}→{offer.destination}, "
                    f"{offer.departure:%b %d %H:%M}) on Google Flights or the "
                    "airline site directly. " + PAYMENT_DISCLAIMER
                ),
            )

        sm.transition(self._repo, trip, TripState.BOOKING_LINK_READY)
        sm.transition(self._repo, trip, TripState.AWAITING_USER_BOOKING)
        dur_h, dur_m = divmod(offer.duration_minutes, 60)
        text = (
            f"Your pick: {offer.carrier_name or offer.carrier} "
            f"{offer.origin}→{offer.destination}, "
            f"{offer.departure:%a %b %d %H:%M} → {offer.arrival:%H:%M} "
            f"({dur_h}h{dur_m:02d}, "
            f"{'nonstop' if offer.connections == 0 else str(offer.connections) + ' stop(s)'}) "
            f"— {offer.total_price} {offer.currency}.\n"
            f"Book here: {offer.booking_url}\n"
            f"{PAYMENT_DISCLAIMER}\n"
            "When you've booked, tell me 'booked' (add the confirmation code "
            "if you like) and I'll save and monitor the trip."
        )
        return HandoffResult(
            trip_id=trip.id,
            state=trip.state.value,
            booking_url=offer.booking_url,
            display_text=text,
        )

    def mark_booked(
        self, trip: Trip, details: Optional[dict[str, Any]] = None
    ) -> BookedTrip:
        """AWAITING_USER_BOOKING -> TRIP_CONFIRMED. Creates a BookedTrip
        from the selected offer plus user-supplied details (confirmation
        code, corrected flight/times). Never payment data."""
        details = dict(details or {})
        offer: Optional[Offer] = (
            self._repo.get_offer(trip.selected_offer_id)
            if trip.selected_offer_id
            else None
        )
        first_number = None
        if offer and offer.segments and offer.segments[0].number:
            first_number = offer.segments[0].number
        booked = BookedTrip(
            user_id=trip.user_id,
            trip_id=trip.id,
            airline=details.get(
                "airline",
                (offer.carrier_name or offer.carrier) if offer else None,
            ),
            flight_number=details.get("flight_number", first_number),
            confirmation_code=details.get("confirmation_code"),
            origin=details.get("origin", offer.origin if offer else None),
            destination=details.get(
                "destination", offer.destination if offer else None
            ),
            departure=details.get("departure", offer.departure if offer else None),
            arrival=details.get("arrival", offer.arrival if offer else None),
            booking_url=details.get(
                "booking_url", offer.booking_url if offer else None
            ),
            price=details.get(
                "price", str(offer.total_price) if offer else None
            ),
            source="manual",
        )
        self._repo.save_booked_trip(booked)
        sm.transition(self._repo, trip, TripState.TRIP_CONFIRMED)
        sm.transition(self._repo, trip, TripState.MONITORING)
        trip.booking_id = booked.id
        self._repo.save_trip(trip)
        return booked
