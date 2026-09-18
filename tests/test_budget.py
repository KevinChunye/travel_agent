from datetime import date, datetime, timezone

from src.models.travel_request import DateRange, PreferenceWeights, TravelRequest
from src.services.search_budget import (
    BudgetConfig,
    SearchBudgetManager,
    SearchCategory,
    search_fingerprint,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_budget(repo, limit=10, reserve=3, now=NOW):
    return SearchBudgetManager(
        repo, BudgetConfig(monthly_limit=limit, reserve=reserve), clock=lambda: now
    )


def spend(budget, n, category=SearchCategory.USER_SEARCH, cache_hit=False):
    for i in range(n):
        budget.record_search(
            category=category, provider="google_flights",
            fingerprint=f"fp{i}", cache_hit=cache_hit,
        )


def test_quota_counting_and_usage(repo):
    b = make_budget(repo)
    spend(b, 4)
    spend(b, 2, cache_hit=True)  # cache hits are free
    usage = b.get_usage()
    assert usage.used == 4
    assert usage.cache_hits == 2
    assert usage.remaining == 6
    assert usage.by_category == {"USER_SEARCH": 4}


def test_reserve_blocks_background_but_not_interactive(repo):
    b = make_budget(repo, limit=10, reserve=3)
    spend(b, 7)  # exactly at limit - reserve
    assert not b.can_search(SearchCategory.PRICE_MONITOR)
    assert not b.can_search(SearchCategory.BACKGROUND_REFRESH)
    assert b.can_search(SearchCategory.USER_SEARCH)
    assert b.can_search(SearchCategory.RETURN_DETAIL)


def test_quota_exhaustion_blocks_everything(repo):
    b = make_budget(repo, limit=5, reserve=2)
    spend(b, 5)
    for cat in SearchCategory:
        assert not b.can_search(cat)
    assert b.remaining_calls() == 0


def test_monthly_rollover(repo):
    clock = {"now": NOW}
    b = SearchBudgetManager(
        repo, BudgetConfig(monthly_limit=5, reserve=1), clock=lambda: clock["now"]
    )
    spend(b, 5)
    assert not b.can_search(SearchCategory.USER_SEARCH)
    clock["now"] = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    assert b.can_search(SearchCategory.USER_SEARCH)
    assert b.get_usage().used == 0


def test_search_cost_estimation(repo, request_bos_jfk):
    b = make_budget(repo, limit=250, reserve=25)
    request_bos_jfk.return_date = DateRange(start=date(2026, 10, 11))
    exact = b.estimate_search_cost(request_bos_jfk)
    assert exact.estimated_calls == 1 and exact.allowed

    flex = b.estimate_search_cost(
        request_bos_jfk, flex_outbound_days=1, flex_return_days=1
    )
    assert flex.date_combinations == 9  # 3 outbound x 3 return
    assert "9 searches" in flex.description

    one_way = TravelRequest(
        origin="BOS", destination="ORD",
        outbound_date=DateRange(start=date(2026, 10, 10)),
    )
    assert b.estimate_search_cost(one_way, flex_outbound_days=1).date_combinations == 3


def test_estimation_respects_remaining_quota(repo, request_bos_jfk):
    b = make_budget(repo, limit=5, reserve=1)
    spend(b, 3)
    plan = b.estimate_search_cost(request_bos_jfk, flex_outbound_days=2)  # 5 calls
    assert not plan.allowed


def test_fingerprint_ignores_weights_and_is_stable(request_bos_jfk):
    fp1 = search_fingerprint(request_bos_jfk)
    request_bos_jfk.weights = PreferenceWeights(price=90, time=5, convenience=3,
                                                reliability=2)
    assert search_fingerprint(request_bos_jfk) == fp1  # rerank != new search
    request_bos_jfk.passengers = 2
    assert search_fingerprint(request_bos_jfk) != fp1
