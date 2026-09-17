"""Duffel flight provider adapter (Stage 2).

Talks to the Duffel API and maps everything into the normalized Offer
model. No Duffel payloads escape this module.

Requires the DUFFEL_API_KEY environment variable. Payment uses the
Duffel balance (or a Duffel payment token) — raw card data never touches
this system.
"""

from __future__ import annotations

import os
from datetime import datetime
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
    TripStatus,
)

_API_BASE = "https://api.duffel.com"
_API_VERSION = "v2"

_CABIN_MAP = {
    CabinClass.ECONOMY: "economy",
    CabinClass.PREMIUM_ECONOMY: "premium_economy",
    CabinClass.BUSINESS: "business",
    CabinClass.FIRST: "first",
}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class DuffelProvider(TravelProvider):
    name = "duffel"
    modes = (TransportMode.FLIGHT,)

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("DUFFEL_API_KEY")
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        if not self._api_key:
            raise ProviderNotConfiguredError("DUFFEL_API_KEY is not set")
        return httpx.Client(
            base_url=_API_BASE,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Duffel-Version": _API_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 404:
            raise OfferNotAvailableError("Duffel resource not found or expired")
        if resp.status_code >= 400:
            # Duffel error bodies are safe to surface (no PII), but keep it short.
            raise ProviderError(f"Duffel API error {resp.status_code}: {resp.text[:500]}")

    # ---- search -----------------------------------------------------------

    def search(self, request: TravelRequest) -> list[Offer]:
        assert request.origin and request.destination and request.outbound_date
        slices: list[dict[str, Any]] = [
            {
                "origin": request.origin,
                "destination": request.destination,
                "departure_date": request.outbound_date.start.isoformat(),
            }
        ]
        if request.return_date is not None:
            slices.append(
                {
                    "origin": request.destination,
                    "destination": request.origin,
                    "departure_date": request.return_date.start.isoformat(),
                }
            )
        passengers = [{"type": t.value} for t in request.passenger_types] or [
            {"type": "adult"}
        ] * request.passengers
        payload = {
            "data": {
                "slices": slices,
                "passengers": passengers,
                "cabin_class": _CABIN_MAP[request.cabin],
            }
        }
        max_connections = request.effective_constraints().effective_max_stops()
        if max_connections is not None:
            payload["data"]["max_connections"] = max_connections

        with self._client() as client:
            resp = client.post(
                "/air/offer_requests", params={"return_offers": "true"}, json=payload
            )
            self._raise_for_status(resp)
            data = resp.json()["data"]
        return [self._offer_from_payload(o) for o in data.get("offers", [])]

    # ---- mapping ----------------------------------------------------------

    def _offer_from_payload(self, payload: dict[str, Any]) -> Offer:
        slices = payload.get("slices", [])
        segments: list[Segment] = []
        connections = 0
        self_transfer = False
        for sl in slices:
            segs = sl.get("segments", [])
            connections = max(connections, max(len(segs) - 1, 0))
            for seg in segs:
                carrier = seg.get("marketing_carrier", {}) or {}
                segments.append(
                    Segment(
                        carrier=carrier.get("iata_code", "??"),
                        carrier_name=carrier.get("name"),
                        number=seg.get("marketing_carrier_flight_number"),
                        origin=(seg.get("origin") or {}).get("iata_code", "?"),
                        destination=(seg.get("destination") or {}).get("iata_code", "?"),
                        departure=_parse_dt(seg.get("departing_at")),
                        arrival=_parse_dt(seg.get("arriving_at")),
                    )
                )

        first_slice = slices[0] if slices else {}
        first_seg = (first_slice.get("segments") or [{}])[0]
        last_seg = (first_slice.get("segments") or [{}])[-1]
        departure = _parse_dt(first_seg.get("departing_at"))
        arrival = _parse_dt(last_seg.get("arriving_at"))
        duration = (
            int((arrival - departure).total_seconds() // 60)
            if departure and arrival
            else 0
        )

        owner = payload.get("owner", {}) or {}
        total = Decimal(payload.get("total_amount", "0"))
        base = Decimal(payload.get("base_amount", payload.get("total_amount", "0")))
        conditions = payload.get("conditions", {}) or {}
        refund = (conditions.get("refund_before_departure") or {})
        change = (conditions.get("change_before_departure") or {})

        carry_on, checked = self._baggage_from_payload(payload)
        restrictions: list[str] = []
        if refund.get("allowed") and refund.get("penalty_amount") not in (None, "0.00"):
            restrictions.append(
                f"Refund penalty {refund.get('penalty_amount')} "
                f"{refund.get('penalty_currency', '')}".strip()
            )
        if not carry_on:
            restrictions.append("No carry-on bag included")

        return Offer(
            provider_offer_id=payload["id"],
            mode=TransportMode.FLIGHT,
            provider=self.name,
            carrier=owner.get("iata_code", "??"),
            carrier_name=owner.get("name"),
            origin=segments[0].origin if segments else "?",
            destination=segments[-1].destination if segments else "?",
            departure=departure,
            arrival=arrival,
            duration_minutes=duration,
            connections=connections,
            self_transfer=self_transfer,
            base_price=base,
            fees=max(total - base, Decimal("0")),
            total_price=total,
            currency=payload.get("total_currency", "USD"),
            baggage=BaggageAllowance(
                carry_on_included=carry_on, checked_bags_included=checked
            ),
            refundable=bool(refund.get("allowed")),
            changeable=bool(change.get("allowed")),
            expires_at=_parse_dt(payload.get("expires_at")),
            restrictions=restrictions,
            segments=segments,
        )

    @staticmethod
    def _baggage_from_payload(payload: dict[str, Any]) -> tuple[bool, int]:
        carry_on = False
        checked = 0
        for sl in payload.get("slices", []):
            for seg in sl.get("segments", []):
                for pax in seg.get("passengers", []):
                    for bag in pax.get("baggages", []):
                        if bag.get("type") == "carry_on" and bag.get("quantity", 0) > 0:
                            carry_on = True
                        if bag.get("type") == "checked":
                            checked = max(checked, int(bag.get("quantity", 0)))
        return carry_on, checked

    # ---- refresh / book / cancel / status ---------------------------------

    def refresh(self, offer: Offer) -> Offer:
        with self._client() as client:
            resp = client.get(f"/air/offers/{offer.provider_offer_id}")
            self._raise_for_status(resp)
            refreshed = self._offer_from_payload(resp.json()["data"])
        refreshed.id = offer.id  # keep the internal identity stable
        return refreshed

    def book(
        self,
        offer: Offer,
        passengers: list[PassengerIdentity],
        payment: PaymentToken,
    ) -> BookingResult:
        pax_payload = []
        for p in passengers:
            entry: dict[str, Any] = {
                "given_name": p.given_name,
                "family_name": p.family_name,
                "type": "adult",
            }
            if p.born_on:
                entry["born_on"] = p.born_on.isoformat()
            if p.gender:
                entry["gender"] = p.gender
            if p.email:
                entry["email"] = p.email
            if p.phone:
                entry["phone_number"] = p.phone
            pax_payload.append(entry)

        payload = {
            "data": {
                "type": "instant",
                "selected_offers": [offer.provider_offer_id],
                "passengers": pax_payload,
                "payments": [
                    {
                        "type": payment.kind,
                        "amount": str(offer.total_price),
                        "currency": offer.currency,
                    }
                ],
            }
        }
        with self._client() as client:
            resp = client.post("/air/orders", json=payload)
            self._raise_for_status(resp)
            data = resp.json()["data"]
        return BookingResult(
            provider_order_id=data["id"],
            booking_reference=data.get("booking_reference"),
            total_price=Decimal(data.get("total_amount", str(offer.total_price))),
            currency=data.get("total_currency", offer.currency),
        )

    def cancel(self, provider_order_id: str) -> CancellationResult:
        with self._client() as client:
            resp = client.post(
                "/air/order_cancellations",
                json={"data": {"order_id": provider_order_id}},
            )
            self._raise_for_status(resp)
            pending = resp.json()["data"]
            resp = client.post(
                f"/air/order_cancellations/{pending['id']}/actions/confirm"
            )
            self._raise_for_status(resp)
            data = resp.json()["data"]
        refund = data.get("refund_amount")
        return CancellationResult(
            provider_order_id=provider_order_id,
            cancelled=True,
            refund_amount=Decimal(refund) if refund else None,
            currency=data.get("refund_currency"),
        )

    def get_status(self, provider_order_id: str) -> StatusResult:
        with self._client() as client:
            resp = client.get(f"/air/orders/{provider_order_id}")
            self._raise_for_status(resp)
            data = resp.json()["data"]
        slices = data.get("slices", [])
        first_seg = (slices[0].get("segments") or [{}])[0] if slices else {}
        last_seg = (slices[0].get("segments") or [{}])[-1] if slices else {}
        cancelled = data.get("cancelled_at") is not None
        return StatusResult(
            provider_order_id=provider_order_id,
            status=TripStatus.CANCELLED if cancelled else TripStatus.ON_TIME,
            departure=_parse_dt(first_seg.get("departing_at")),
            arrival=_parse_dt(last_seg.get("arriving_at")),
            origin_terminal=(first_seg.get("origin") or {}).get("terminal")
            if isinstance(first_seg.get("origin"), dict)
            else None,
            destination_terminal=(last_seg.get("destination") or {}).get("terminal")
            if isinstance(last_seg.get("destination"), dict)
            else None,
        )
