"""Structured representation of a travel request.

The LLM parses natural language into this model; everything downstream
(search, filtering, ranking, booking) works only with these structures.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from enum import Enum
from typing import ClassVar, Optional

from pydantic import BaseModel, Field, model_validator


class TransportMode(str, Enum):
    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"


class CabinClass(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class PassengerType(str, Enum):
    ADULT = "adult"
    CHILD = "child"
    INFANT = "infant"


class DateRange(BaseModel):
    """A single date or a flexible range (start == end for a fixed date)."""

    start: date
    end: date

    @model_validator(mode="before")
    @classmethod
    def _default_end(cls, values):
        if isinstance(values, dict) and values.get("end") is None:
            values = {**values, "end": values.get("start")}
        return values

    @model_validator(mode="after")
    def _check_order(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("DateRange end must not be before start")
        return self

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


class TimeWindow(BaseModel):
    """Acceptable local departure time window. Open ends are None."""

    earliest: Optional[time] = None
    latest: Optional[time] = None

    def contains(self, t: time) -> bool:
        if self.earliest is not None and t < self.earliest:
            return False
        if self.latest is not None and t > self.latest:
            return False
        return True

    def is_open(self) -> bool:
        return self.earliest is None and self.latest is None


class BaggageRequirement(BaseModel):
    carry_on: bool = True
    checked_bags: int = 0


class HardConstraints(BaseModel):
    """Constraints that eliminate offers outright (never traded off)."""

    max_price: Optional[float] = Field(default=None, gt=0)
    max_stops: Optional[int] = Field(default=None, ge=0)
    nonstop_only: bool = False
    no_red_eye: bool = False
    carry_on_required: bool = False
    checked_bag_required: bool = False
    refundable_required: bool = False
    arrive_by: Optional[datetime] = None
    depart_after: Optional[datetime] = None

    def effective_max_stops(self) -> Optional[int]:
        if self.nonstop_only:
            return 0
        return self.max_stops


_WEIGHT_KEYS = ("price", "time", "convenience", "reliability")
_WEIGHT_ALIASES = {
    "price": "price",
    "cost": "price",
    "cheap": "price",
    "time": "time",
    "speed": "time",
    "duration": "time",
    "convenience": "convenience",
    "comfort": "convenience",
    "reliability": "reliability",
    "flexibility": "reliability",
}


class PreferenceWeights(BaseModel):
    """Ranking weights. Stored as raw values; use normalized() when scoring."""

    price: float = Field(default=40, ge=0)
    time: float = Field(default=30, ge=0)
    convenience: float = Field(default=20, ge=0)
    reliability: float = Field(default=10, ge=0)

    def normalized(self) -> dict[str, float]:
        total = self.price + self.time + self.convenience + self.reliability
        if total <= 0:
            return {"price": 0.4, "time": 0.3, "convenience": 0.2, "reliability": 0.1}
        return {
            "price": self.price / total,
            "time": self.time / total,
            "convenience": self.convenience / total,
            "reliability": self.reliability / total,
        }

    @classmethod
    def parse_overrides(
        cls, text: str, base: Optional["PreferenceWeights"] = None
    ) -> "PreferenceWeights":
        """Parse overrides like ``price 60, time 25, convenience 15``.

        Deterministic rules:
        - Named components take the given values (``price=60`` and ``price: 60``
          also work; aliases like cost/speed are accepted).
        - Unmentioned components share the remainder up to 100 in proportion to
          their weights in ``base`` (defaults if no base). If the mentioned
          values already reach 100, unmentioned components become 0.
        """
        base = base or cls()
        found: dict[str, float] = {}
        for m in re.finditer(r"([a-zA-Z]+)\s*[:=]?\s*(\d+(?:\.\d+)?)", text):
            key = _WEIGHT_ALIASES.get(m.group(1).lower())
            if key:
                found[key] = float(m.group(2))
        if not found:
            raise ValueError(f"No weight components found in: {text!r}")

        mentioned_total = sum(found.values())
        remainder = max(0.0, 100.0 - mentioned_total)
        rest_keys = [k for k in _WEIGHT_KEYS if k not in found]
        base_rest_total = sum(getattr(base, k) for k in rest_keys)
        values = dict(found)
        for k in rest_keys:
            if remainder > 0 and base_rest_total > 0:
                values[k] = remainder * getattr(base, k) / base_rest_total
            else:
                values[k] = 0.0
        return cls(**values)


class TravelRequest(BaseModel):
    """Everything the deterministic pipeline needs to search and rank."""

    origin: Optional[str] = None
    destination: Optional[str] = None
    outbound_date: Optional[DateRange] = None
    outbound_window: TimeWindow = Field(default_factory=TimeWindow)
    return_date: Optional[DateRange] = None
    return_window: TimeWindow = Field(default_factory=TimeWindow)
    passengers: int = Field(default=1, ge=1)
    passenger_types: list[PassengerType] = Field(
        default_factory=lambda: [PassengerType.ADULT]
    )
    modes: list[TransportMode] = Field(default_factory=lambda: [TransportMode.FLIGHT])
    cabin: CabinClass = CabinClass.ECONOMY
    baggage: BaggageRequirement = Field(default_factory=BaggageRequirement)
    max_price: Optional[float] = Field(default=None, gt=0)
    max_stops: Optional[int] = Field(default=None, ge=0)
    nearby_airports_allowed: bool = True
    constraints: HardConstraints = Field(default_factory=HardConstraints)
    weights: Optional[PreferenceWeights] = None
    notes: Optional[str] = None

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ("origin", "destination", "outbound_date")

    def missing_required_fields(self) -> list[str]:
        """Fields the agent still needs to ask the user for."""
        missing = [f for f in self.REQUIRED_FIELDS if getattr(self, f) is None]
        return missing

    def is_round_trip(self) -> bool:
        return self.return_date is not None

    def effective_constraints(self) -> HardConstraints:
        """Merge top-level shortcuts (max_price/max_stops) into constraints."""
        c = self.constraints.model_copy()
        if c.max_price is None and self.max_price is not None:
            c.max_price = self.max_price
        if c.max_stops is None and self.max_stops is not None:
            c.max_stops = self.max_stops
        if self.baggage.checked_bags > 0:
            c.checked_bag_required = True
        return c
