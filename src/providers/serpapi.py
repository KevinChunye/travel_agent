"""Backwards-compatibility alias.

The SerpAPI adapter grew into the full GoogleFlightsProvider (budget
management, caching, cabin/stops/time-window mapping). Import from
``src.providers.google_flights`` in new code.
"""

from src.providers.google_flights import (  # noqa: F401
    GoogleFlightsProvider,
    GoogleFlightsProvider as SerpApiFlightsProvider,
)
