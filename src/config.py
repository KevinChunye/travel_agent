"""Configuration from environment variables (no secrets in code or files).

See .env.example for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/travel_agent.sqlite3"
    providers: tuple[str, ...] = ("mock",)
    default_currency: str = "USD"
    duffel_api_key: str | None = None
    serpapi_api_key: str | None = None
    serpapi_monthly_limit: int = 250
    serpapi_reserve: int = 25
    search_cache_ttl_minutes: int = 360
    dashboard_port: int = 8090
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
            serpapi_api_key=os.environ.get("SERPAPI_API_KEY"),
            serpapi_monthly_limit=int(os.environ.get("SERPAPI_MONTHLY_LIMIT", "250")),
            serpapi_reserve=int(os.environ.get("SERPAPI_RESERVE", "25")),
            search_cache_ttl_minutes=int(
                os.environ.get("SEARCH_CACHE_TTL_MINUTES", "360")
            ),
            dashboard_port=int(os.environ.get("DASHBOARD_PORT", "8090")),
            price_buffer_pct=float(os.environ.get("PRICE_BUFFER_PCT", "0")),
        )


def build_providers(settings: Settings, repo=None, budget=None):
    """Instantiate the configured provider adapters.

    ``repo``/``budget`` wire the Google Flights provider into the
    persistent cache and the SerpAPI search-budget manager.
    """
    from src.providers.distribusion import DistribusionProvider
    from src.providers.duffel import DuffelProvider
    from src.providers.google_flights import GoogleFlightsProvider
    from src.providers.mock import MockFlightProvider
    from src.providers.trainline import TrainlineProvider

    def _google_flights():
        return GoogleFlightsProvider(
            api_key=settings.serpapi_api_key,
            repo=repo,
            budget=budget,
            cache_ttl_minutes=settings.search_cache_ttl_minutes,
        )

    registry = {
        "mock": lambda: MockFlightProvider(),
        "duffel": lambda: DuffelProvider(api_key=settings.duffel_api_key),
        "google_flights": _google_flights,
        "serpapi": _google_flights,  # legacy name
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
