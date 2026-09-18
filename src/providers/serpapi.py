"""SerpApi Google Flights provider (search-only, free tier friendly).

Provides live flight search via SerpApi's Google Flights engine
(https://serpapi.com/google-flights-api). It cannot book — Google
Flights is a metasearch — so ``book``/``cancel``/``get_status`` raise a
clear error, and every offer carries a Google Flights ``booking_url``
for the user to complete the purchase themselves. Real booking remains
the Duffel provider's job.

Quota notes (free tier ~250 searches/month): a trip's initial search is
one API call; conversational refinements (cheaper / nonstop only /
weights) re-rank the cached offers with zero extra calls; a refresh
before "booking" costs one call.

Requires the SERPAPI_API_KEY environment variable.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx

from src.models.booking import PassengerIdentity, PaymentToken
from src.models.offer import BaggageAllowance, Offer, Segment
from src.models.travel_request import TransportMode, TravelRequest
from src.providers.base import (
    BookingResult,
    CancellationResult,
    OfferNotAvailableError,
    ProviderError,
    ProviderNotConfiguredError,
    StatusResult,
    TravelProvider,
)

_API_URL = "https://serpapi.com/search.json"
_SEARCH_ONLY_MSG = (
    "SerpApi/Google Flights is a search-only provider. Book via the offer's "
    "booking_url, or enable a bookable provider (TRAVEL_PROVIDERS=duffel)."
)


def _parse_dt(value: str) -> datetime:
    # SerpApi uses "2026-10-01 06:30" (airport-local time).
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


class SerpApiFlightsProvider(TravelProvider):
    name = "serpapi"
    modes = (TransportMode.FLIGHT,)

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("SERPAPI_API_KEY")
        self._timeout = timeout

    # -- HTTP ----------------------------------------------------------------

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise ProviderNotConfiguredError("SERPAPI_API_KEY is not set")
        query = {
            "engine": "google_flights",
            "hl": "en",
            "currency": "USD",
            "api_key": self._api_key,
            **params,
        }
        try:
            resp = httpx.get(_API_URL, params=query, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(f"SerpApi request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"SerpApi error {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        if data.get("error"):
            raise ProviderError(f"SerpApi error: {data['error']}")
        return data

    # -- search --------------------------------------------------------------

    def _search_params(self, request: TravelRequest) -> dict[str, Any]:
        assert request.origin and request.destination and request.outbound_date
        params: dict[str, Any] = {
            "departure_id": request.origin,
            "arrival_id": request.destination,
            "outbound_date": request.outbound_date.start.isoformat(),
            "adults": request.passengers,
        }
        if request.return_date is not None:
            params["type"] = 1  # round trip
            params["return_date"] = request.return_date.start.isoformat()
        else:
            params["type"] = 2  # one way
        return params

    def search(self, request: TravelRequest) -> list[Offer]:
        params = self._search_params(request)
        data = self._get(params)
        items = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        search_url = (data.get("search_metadata") or {}).get(
            "google_flights_url"
        )
        offers = []
        for item in items:
            offer = self._offer_from_item(item, params, search_url)
            if offer is not None:
                offers.append(offer)
        return offers

    def _offer_from_item(
        self,
        item: dict[str, Any],
        search_params: dict[str, Any],
        search_url: Optional[str],
    ) -> Optional[Offer]:
        flights = item.get("flights") or []
        if not flights or item.get("price") in (None, ""):
            return None

        segments: list[Segment] = []
        for f in flights:
            number = f.get("flight_number", "")
            code_match = re.match(r"([A-Z0-9]{2,3})\s", number)
            segments.append(
                Segment(
                    carrier=code_match.group(1) if code_match else (f.get("airline") or "??"),
                    carrier_name=f.get("airline"),
                    number=number or None,
                    origin=(f.get("departure_airport") or {}).get("id", "?"),
                    destination=(f.get("arrival_airport") or {}).get("id", "?"),
                    departure=_parse_dt(
                        (f.get("departure_airport") or {}).get("time")
                    ),
                    arrival=_parse_dt((f.get("arrival_airport") or {}).get("time")),
                )
            )

        total_minutes = int(item.get("total_duration") or 0)
        price = Decimal(str(item.get("price")))
        layovers = item.get("layovers") or []
        fingerprint_src = "|".join(
            f"{s.number}@{s.departure.isoformat()}" for s in segments
        )
        offer_id_src = hashlib.sha256(fingerprint_src.encode()).hexdigest()[:16]

        restrictions = ["Search-only result: book via Google Flights link"]
        if item.get("type") == "Round trip" or search_params.get("type") == 1:
            restrictions.append(
                "Price covers the round trip; outbound leg shown, return "
                "selected at booking time"
            )

        return Offer(
            provider_offer_id=item.get("booking_token") or f"serp_{offer_id_src}",
            mode=TransportMode.FLIGHT,
            provider=self.name,
            carrier=segments[0].carrier,
            carrier_name=segments[0].carrier_name,
            origin=segments[0].origin,
            destination=segments[-1].destination,
            departure=segments[0].departure,
            arrival=segments[-1].arrival,
            duration_minutes=total_minutes
            or int((segments[-1].arrival - segments[0].departure).total_seconds() // 60),
            connections=len(layovers) if layovers else max(len(segments) - 1, 0),
            self_transfer=False,
            base_price=price,
            fees=Decimal("0"),
            total_price=price,
            currency="USD",
            baggage=BaggageAllowance(
                carry_on_included=True,  # Google prices include a carry-on
                notes="Checked-bag fees not included in metasearch price",
            ),
            refundable=None,
            changeable=None,
            expires_at=None,
            restrictions=restrictions,
            segments=segments,
            provider_meta={"search_params": {k: v for k, v in search_params.items()}},
            booking_url=search_url,
        )

    # -- refresh -------------------------------------------------------------

    def refresh(self, offer: Offer) -> Offer:
        """Re-run the original search and re-match this itinerary by its
        flight numbers and departure times; update the price if found."""
        params = (offer.provider_meta or {}).get("search_params")
        if not params:
            raise OfferNotAvailableError(
                "Cannot refresh: original search parameters missing"
            )
        data = self._get(params)
        items = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        search_url = (data.get("search_metadata") or {}).get("google_flights_url")
        wanted = offer.itinerary_fingerprint()
        for item in items:
            candidate = self._offer_from_item(item, params, search_url)
            if candidate is not None and candidate.itinerary_fingerprint() == wanted:
                candidate.id = offer.id
                return candidate
        raise OfferNotAvailableError(
            "This itinerary no longer appears in Google Flights results"
        )

    # -- unsupported operations ----------------------------------------------

    def book(
        self,
        offer: Offer,
        passengers: list[PassengerIdentity],
        payment: PaymentToken,
    ) -> BookingResult:
        raise ProviderNotConfiguredError(_SEARCH_ONLY_MSG)

    def cancel(self, provider_order_id: str) -> CancellationResult:
        raise ProviderNotConfiguredError(_SEARCH_ONLY_MSG)

    def get_status(self, provider_order_id: str) -> StatusResult:
        raise ProviderNotConfiguredError(_SEARCH_ONLY_MSG)
