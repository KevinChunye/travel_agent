"""Google Flights provider via SerpAPI (search-only, budget-managed).

This is the active flight-search provider. It:
- searches Google Flights through SerpAPI (one-way and round-trip,
  passengers, cabin class, stop constraints, departure-time windows),
- normalizes results into the internal Offer model (raw SerpAPI payloads
  never leave this module),
- routes every network call through the SearchBudgetManager so the
  monthly quota can never be exceeded automatically,
- serves repeated equivalent searches from the persistent cache for free.

It cannot book: Google Flights is a metasearch. ``book``/``cancel``/
``get_status`` raise, and every offer carries a booking_url where the
user completes the purchase themselves.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from src.models.booking import PassengerIdentity, PaymentToken
from src.models.offer import BaggageAllowance, Offer, Segment
from src.models.travel_request import CabinClass, TransportMode, TravelRequest
from src.providers.base import (
    BookingResult,
    CancellationResult,
    OfferNotAvailableError,
    ProviderError,
    ProviderNotConfiguredError,
    StatusResult,
    TravelProvider,
)
from src.services.search_budget import (
    SearchBudgetManager,
    SearchCategory,
    SearchQuotaExceededError,
    search_fingerprint,
)

_API_URL = "https://serpapi.com/search.json"
_SEARCH_ONLY_MSG = (
    "Google Flights is a search-only provider: there is no in-agent "
    "booking or payment. Use the offer's booking_url to purchase on "
    "Google Flights / the airline site."
)

# SerpAPI travel_class: 1 economy, 2 premium economy, 3 business, 4 first.
_CABIN_MAP = {
    CabinClass.ECONOMY: 1,
    CabinClass.PREMIUM_ECONOMY: 2,
    CabinClass.BUSINESS: 3,
    CabinClass.FIRST: 4,
}


def _parse_dt(value: str) -> datetime:
    # SerpAPI uses "2026-10-01 06:30" (airport-local time).
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


class GoogleFlightsProvider(TravelProvider):
    name = "google_flights"
    modes = (TransportMode.FLIGHT,)
    supports_booking = False

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        repo=None,
        budget: Optional[SearchBudgetManager] = None,
        cache_ttl_minutes: int = 360,
        timeout: float = 30.0,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._api_key = api_key or os.environ.get("SERPAPI_API_KEY")
        self._repo = repo
        self._budget = budget
        self._cache_ttl_minutes = cache_ttl_minutes
        self._timeout = timeout
        self._clock = clock

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
            raise ProviderError(f"SerpAPI request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"SerpAPI error {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        if data.get("error"):
            raise ProviderError(f"SerpAPI error: {data['error']}")
        return data

    # -- request mapping -----------------------------------------------------

    def _search_params(self, request: TravelRequest) -> dict[str, Any]:
        assert request.origin and request.destination and request.outbound_date
        params: dict[str, Any] = {
            "departure_id": request.origin,
            "arrival_id": request.destination,
            "outbound_date": request.outbound_date.start.isoformat(),
            "adults": request.passengers,
            "travel_class": _CABIN_MAP[request.cabin],
        }
        if request.return_date is not None:
            params["type"] = 1  # round trip
            params["return_date"] = request.return_date.start.isoformat()
        else:
            params["type"] = 2  # one way
        # Stop constraints: SerpAPI stops: 1 nonstop, 2 <=1 stop, 3 <=2 stops.
        max_stops = request.effective_constraints().effective_max_stops()
        if max_stops == 0:
            params["stops"] = 1
        elif max_stops == 1:
            params["stops"] = 2
        elif max_stops == 2:
            params["stops"] = 3
        # Departure-time window: "earliestHour,latestHour" (outbound leg).
        window = request.outbound_window
        if window.earliest is not None or window.latest is not None:
            earliest = window.earliest.hour if window.earliest else 0
            latest = window.latest.hour if window.latest else 23
            params["outbound_times"] = f"{earliest},{latest}"
        return params

    # -- search (budget- and cache-managed) ----------------------------------

    def search(self, request: TravelRequest) -> list[Offer]:
        return self.search_with_options(request)

    def search_with_options(
        self,
        request: TravelRequest,
        *,
        category: SearchCategory = SearchCategory.USER_SEARCH,
        force_refresh: bool = False,
        trip_id: Optional[str] = None,
    ) -> list[Offer]:
        fingerprint = search_fingerprint(request)
        now = self._clock()

        # 1. Cache: an equivalent recent search is free.
        if not force_refresh and self._repo is not None:
            cached = self._repo.cache_get(
                fingerprint, self._cache_ttl_minutes, now
            )
            if cached is not None:
                if self._budget is not None:
                    self._budget.record_search(
                        category=category,
                        provider=self.name,
                        fingerprint=fingerprint,
                        trip_id=trip_id,
                        cache_hit=True,
                    )
                return cached

        # 2. Budget gate: never exceed the monthly quota automatically.
        if self._budget is not None and not self._budget.can_search(category):
            raise SearchQuotaExceededError(
                f"Monthly search quota would be exceeded "
                f"(category {category.value}); not calling SerpAPI."
            )

        # 3. The one place in the codebase that talks to SerpAPI.
        params = self._search_params(request)
        data = self._get(params)
        if self._budget is not None:
            self._budget.record_search(
                category=category,
                provider=self.name,
                fingerprint=fingerprint,
                trip_id=trip_id,
                cache_hit=False,
            )

        items = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        search_url = (data.get("search_metadata") or {}).get("google_flights_url")
        offers = []
        for item in items:
            offer = self._offer_from_item(item, params, search_url)
            if offer is not None:
                offers.append(offer)

        if self._repo is not None:
            self._repo.cache_put(fingerprint, self.name, offers, now)
        return offers

    # -- normalization -------------------------------------------------------

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
                    carrier=code_match.group(1)
                    if code_match
                    else (f.get("airline") or "??"),
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

        restrictions = ["Search-only result: book via Google Flights link"]
        for lay in layovers:
            mins = lay.get("duration")
            where = lay.get("id") or lay.get("name") or "?"
            if mins:
                restrictions.append(f"Layover in {where} ({mins} min)")
        if item.get("type") == "Round trip" or search_params.get("type") == 1:
            restrictions.append(
                "Price covers the round trip; outbound leg shown, return "
                "selected at booking time"
            )

        provider_meta: dict[str, Any] = {
            "search_params": dict(search_params),
        }
        emissions = item.get("carbon_emissions") or {}
        if emissions.get("this_flight"):
            provider_meta["emissions_g"] = emissions["this_flight"]
            if emissions.get("difference_percent") is not None:
                provider_meta["emissions_vs_typical_pct"] = emissions[
                    "difference_percent"
                ]

        fingerprint_src = "|".join(
            f"{s.number}@{s.departure.isoformat()}" for s in segments
        )
        offer_id_src = hashlib.sha256(fingerprint_src.encode()).hexdigest()[:16]

        return Offer(
            provider_offer_id=item.get("booking_token") or f"gf_{offer_id_src}",
            mode=TransportMode.FLIGHT,
            provider=self.name,
            carrier=segments[0].carrier,
            carrier_name=segments[0].carrier_name,
            origin=segments[0].origin,
            destination=segments[-1].destination,
            departure=segments[0].departure,
            arrival=segments[-1].arrival,
            duration_minutes=total_minutes
            or int(
                (segments[-1].arrival - segments[0].departure).total_seconds() // 60
            ),
            connections=len(layovers) if layovers else max(len(segments) - 1, 0),
            self_transfer=False,
            base_price=price,
            fees=Decimal("0"),
            total_price=price,
            currency="USD",
            baggage=BaggageAllowance(
                carry_on_included=True,
                notes="Checked-bag fees not included in metasearch price",
            ),
            refundable=None,
            changeable=None,
            expires_at=None,
            restrictions=restrictions,
            segments=segments,
            provider_meta=provider_meta,
            booking_url=search_url,
        )

    # -- refresh -------------------------------------------------------------

    def refresh(self, offer: Offer) -> Offer:
        """Re-run the original search (budget-gated, cache bypassed) and
        re-match this itinerary by flight numbers and departure times."""
        params = (offer.provider_meta or {}).get("search_params")
        if not params:
            raise OfferNotAvailableError(
                "Cannot refresh: original search parameters missing"
            )
        if self._budget is not None and not self._budget.can_search(
            SearchCategory.RETURN_DETAIL
        ):
            raise SearchQuotaExceededError(
                "Monthly search quota would be exceeded; not refreshing."
            )
        data = self._get(params)
        if self._budget is not None:
            self._budget.record_search(
                category=SearchCategory.RETURN_DETAIL,
                provider=self.name,
                fingerprint=f"refresh:{offer.id}",
                cache_hit=False,
            )
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
