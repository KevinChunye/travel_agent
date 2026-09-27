"""Airport-local times to absolute (UTC) times.

Google Flights (via SerpAPI) reports departures as naive airport-local
times ("2026-10-01 06:30"). That is right for display, but anything that
schedules against the real clock (reminders) must first convert to UTC
using the airport's own time zone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _airports() -> dict:
    try:
        import airportsdata
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        logger.warning("airportsdata not installed; airport times treated as UTC")
        return {}
    return airportsdata.load("IATA")


def airport_timezone(code: Optional[str]) -> Optional[ZoneInfo]:
    """IANA time zone for an IATA airport code, or None if unknown."""
    if not code:
        return None
    entry = _airports().get(code.strip().upper())
    if not entry or not entry.get("tz"):
        return None
    try:
        return ZoneInfo(entry["tz"])
    except ZoneInfoNotFoundError:
        return None


def local_to_utc(value: datetime, airport: Optional[str]) -> datetime:
    """Interpret a naive airport-local time at ``airport`` and return UTC.

    Aware datetimes are already absolute and are only normalized. When the
    airport is unknown the time is treated as UTC (the old behavior) and a
    warning is logged.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    tz = airport_timezone(airport)
    if tz is None:
        logger.warning("unknown time zone for airport %r; treating %s as UTC",
                       airport, value.isoformat())
        return value.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=tz).astimezone(timezone.utc)
