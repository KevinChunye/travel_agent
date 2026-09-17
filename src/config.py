"""Configuration from environment variables (no secrets in code or files).

See .env.example for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/travel_agent.sqlite3"
    providers: tuple[str, ...] = ("mock",)
    default_currency: str = "USD"
    duffel_api_key: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    price_buffer_pct: float = 0.0  # tolerated price drift vs approved total

    @classmethod
    def from_env(cls) -> "Settings":
        providers = tuple(
            p.strip()
            for p in os.environ.get("TRAVEL_PROVIDERS", "mock").split(",")
            if p.strip()
        )
        return cls(
            database_path=os.environ.get("DATABASE_PATH", "data/travel_agent.sqlite3"),
            providers=providers,
            default_currency=os.environ.get("DEFAULT_CURRENCY", "USD"),
            duffel_api_key=os.environ.get("DUFFEL_API_KEY"),
            whatsapp_access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN"),
            whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID"),
            price_buffer_pct=float(os.environ.get("PRICE_BUFFER_PCT", "0")),
        )


def build_providers(settings: Settings):
    """Instantiate the configured provider adapters."""
    from src.providers.distribusion import DistribusionProvider
    from src.providers.duffel import DuffelProvider
    from src.providers.mock import MockFlightProvider
    from src.providers.trainline import TrainlineProvider

    registry = {
        "mock": lambda: MockFlightProvider(),
        "duffel": lambda: DuffelProvider(api_key=settings.duffel_api_key),
        "trainline": lambda: TrainlineProvider(),
        "distribusion": lambda: DistribusionProvider(),
    }
    providers = []
    for name in settings.providers:
        factory = registry.get(name)
        if factory is None:
            raise ValueError(f"Unknown provider {name!r} in TRAVEL_PROVIDERS")
        providers.append(factory())
    return providers
