"""Persistent per-user preferences, kept separate from individual trips.

Trip-specific instructions ("this time, business class") live on the
TravelRequest; these are the durable defaults.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.models.travel_request import CabinClass, PreferenceWeights


class SeatPreference(str, Enum):
    WINDOW = "window"
    AISLE = "aisle"
    NO_PREFERENCE = "no_preference"


class RedEyePreference(str, Enum):
    NEVER = "never"
    AVOID = "avoid"
    OK = "ok"


class UserPreferences(BaseModel):
    user_id: str
    home_city: Optional[str] = None
    home_airport: Optional[str] = None
    preferred_airports: list[str] = Field(default_factory=list)
    allowed_nearby_airports: list[str] = Field(default_factory=list)
    preferred_airlines: list[str] = Field(default_factory=list)
    preferred_rail_services: list[str] = Field(default_factory=list)
    seat_preference: SeatPreference = SeatPreference.NO_PREFERENCE
    cabin_preference: CabinClass = CabinClass.ECONOMY
    default_carry_on: bool = True
    default_checked_bags: int = 0
    earliest_normal_departure: Optional[time] = None
    latest_normal_arrival: Optional[time] = None
    red_eye: RedEyePreference = RedEyePreference.AVOID
    max_normal_connections: int = 1
    # Sensitivities double as default ranking weights (see default_weights()).
    price_sensitivity: float = Field(default=40, ge=0)
    time_sensitivity: float = Field(default=30, ge=0)
    convenience_sensitivity: float = Field(default=20, ge=0)
    reliability_sensitivity: float = Field(default=10, ge=0)

    def default_weights(self) -> PreferenceWeights:
        return PreferenceWeights(
            price=self.price_sensitivity,
            time=self.time_sensitivity,
            convenience=self.convenience_sensitivity,
            reliability=self.reliability_sensitivity,
        )


class SelectionFeedback(BaseModel):
    """Record of which option a user picked, for future preference learning.

    We only record; no learning logic yet (by design).
    """

    user_id: str
    trip_id: str
    selected_offer_id: str
    presented_offer_ids: list[str]
    selected_label: Optional[str] = None  # e.g. "best", "cheapest", "fastest"
    utility_scores: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
