"""SerpAPI search-budget management.

The monthly SerpAPI allowance (~250 searches) is a first-class system
resource. Every Google Flights request must pass through this manager:
the provider refuses to hit the network unless ``can_search()`` allows
it, and every call (including cache hits, which are free) is recorded.

Rules:
- The monthly limit is a hard ceiling; it is never exceeded automatically.
- A configurable reserve is kept for interactive use: background
  categories (PRICE_MONITOR, BACKGROUND_REFRESH) may never consume it.
- Category budgets are advisory configuration defaults used for planning
  and reporting; unused capacity in one category remains available.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.models.travel_request import TravelRequest


class SearchCategory(str, Enum):
    USER_SEARCH = "USER_SEARCH"
    RETURN_DETAIL = "RETURN_DETAIL"
    FLEXIBLE_DATE = "FLEXIBLE_DATE"
    PRICE_MONITOR = "PRICE_MONITOR"
    BACKGROUND_REFRESH = "BACKGROUND_REFRESH"


#: Categories that must never consume the emergency reserve.
BACKGROUND_CATEGORIES = {
    SearchCategory.PRICE_MONITOR,
    SearchCategory.BACKGROUND_REFRESH,
}


class BudgetConfig(BaseModel):
    monthly_limit: int = 250
    reserve: int = 25
    # Advisory planning defaults; not hard per-category walls.
    category_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            SearchCategory.USER_SEARCH.value: 70,
            SearchCategory.RETURN_DETAIL.value: 50,
            SearchCategory.PRICE_MONITOR.value: 80,
            SearchCategory.FLEXIBLE_DATE.value: 25,
            "RESERVE": 25,
        }
    )


class ApiCallRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"api_{uuid.uuid4().hex[:12]}")
    provider: str
    category: SearchCategory
    fingerprint: str
    trip_id: Optional[str] = None
    cache_hit: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class BudgetUsage(BaseModel):
    month: str
    limit: int
    reserve: int
    used: int  # live API calls only (cache hits are free)
    cache_hits: int
    by_category: dict[str, int]
    remaining: int
    remaining_for_background: int


class SearchPlan(BaseModel):
    """Cost estimate for a (possibly flexible) search, shown to the user
    before anything expensive runs."""

    date_combinations: int
    estimated_calls: int
    category: SearchCategory
    allowed: bool
    description: str


class SearchQuotaExceededError(Exception):
    """Raised instead of making an API call that would exceed the budget."""


def search_fingerprint(request: TravelRequest) -> str:
    """Deterministic fingerprint of the fields that define an equivalent
    search. Preference weights are deliberately excluded — reranking must
    never look like a new search."""
    c = request.effective_constraints()
    key = {
        "origin": (request.origin or "").upper(),
        "destination": (request.destination or "").upper(),
        "out": request.outbound_date.start.isoformat() if request.outbound_date else None,
        "out_end": request.outbound_date.end.isoformat() if request.outbound_date else None,
        "ret": request.return_date.start.isoformat() if request.return_date else None,
        "pax": request.passengers,
        "cabin": request.cabin.value,
        "max_stops": c.effective_max_stops(),
        "earliest": request.outbound_window.earliest.isoformat()
        if request.outbound_window.earliest else None,
        "latest": request.outbound_window.latest.isoformat()
        if request.outbound_window.latest else None,
    }
    raw = json.dumps(key, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


class SearchBudgetManager:
    def __init__(
        self,
        repo,
        config: Optional[BudgetConfig] = None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repo = repo
        self.config = config or BudgetConfig()
        self._clock = clock

    # -- accounting ----------------------------------------------------------

    def get_usage(self) -> BudgetUsage:
        now = self._clock()
        records = self._repo.api_calls_for_month(_month_key(now))
        live = [r for r in records if not r.cache_hit]
        by_category: dict[str, int] = {}
        for r in live:
            by_category[r.category.value] = by_category.get(r.category.value, 0) + 1
        used = len(live)
        cfg = self.config
        return BudgetUsage(
            month=_month_key(now),
            limit=cfg.monthly_limit,
            reserve=cfg.reserve,
            used=used,
            cache_hits=len(records) - used,
            by_category=by_category,
            remaining=max(cfg.monthly_limit - used, 0),
            remaining_for_background=max(
                cfg.monthly_limit - cfg.reserve - used, 0
            ),
        )

    def remaining_calls(self, include_reserve: bool = True) -> int:
        usage = self.get_usage()
        return usage.remaining if include_reserve else usage.remaining_for_background

    def can_search(self, category: SearchCategory, calls: int = 1) -> bool:
        usage = self.get_usage()
        if category in BACKGROUND_CATEGORIES:
            return usage.remaining_for_background >= calls
        return usage.remaining >= calls

    def record_search(
        self,
        *,
        category: SearchCategory,
        provider: str,
        fingerprint: str,
        trip_id: Optional[str] = None,
        cache_hit: bool = False,
    ) -> ApiCallRecord:
        record = ApiCallRecord(
            provider=provider,
            category=category,
            fingerprint=fingerprint,
            trip_id=trip_id,
            cache_hit=cache_hit,
            created_at=self._clock(),
        )
        self._repo.record_api_call(record)
        return record

    # -- planning ------------------------------------------------------------

    def estimate_search_cost(
        self,
        request: TravelRequest,
        *,
        flex_outbound_days: int = 0,
        flex_return_days: int = 0,
        category: SearchCategory = SearchCategory.USER_SEARCH,
    ) -> SearchPlan:
        """Estimated API calls for a search before running it.

        Exact dates cost 1 call. Every +/-N day of flexibility multiplies
        the date combinations (each combination is one Google Flights
        search). Nothing here executes a search.
        """
        out_options = 1 + 2 * max(flex_outbound_days, 0)
        ret_options = (
            1 + 2 * max(flex_return_days, 0) if request.return_date else 1
        )
        combos = out_options * ret_options
        allowed = self.can_search(category, calls=combos)
        if combos == 1:
            description = "Exact dates: 1 search."
        else:
            description = (
                f"Flexible dates would use approximately {combos} searches "
                f"({out_options} outbound x {ret_options} return date option(s))."
            )
        if not allowed:
            description += (
                f" Not enough quota remaining "
                f"({self.remaining_calls(category not in BACKGROUND_CATEGORIES)} left)."
            )
        return SearchPlan(
            date_combinations=combos,
            estimated_calls=combos,
            category=category,
            allowed=allowed,
            description=description,
        )
