from datetime import datetime, time, timedelta, timezone

from src.models.travel_request import (
    HardConstraints,
    PreferenceWeights,
    TimeWindow,
    TravelRequest,
)
from src.ranking.scorer import (
    apply_hard_constraints,
    rank_offers,
    select_presentation,
)
from tests.conftest import NOW, make_offer


def test_price_weighting_prefers_cheapest(request_bos_jfk):
    cheap_slow = make_offer(price="80", duration=300, connections=1, dep_hour=7)
    fast_pricey = make_offer(price="400", duration=70, dep_hour=10)
    offers = [cheap_slow, fast_pricey]

    price_first = rank_offers(
        offers, PreferenceWeights(price=90, time=5, convenience=3, reliability=2),
        request_bos_jfk,
    )
    assert price_first[0].offer.id == cheap_slow.id

    time_first = rank_offers(
        offers, PreferenceWeights(price=5, time=90, convenience=3, reliability=2),
        request_bos_jfk,
    )
    assert time_first[0].offer.id == fast_pricey.id


def test_scores_are_normalized_and_explained(request_bos_jfk):
    offers = [
        make_offer(price="100", duration=80, dep_hour=9),
        make_offer(price="200", duration=160, dep_hour=13, carrier="AA"),
        make_offer(price="300", duration=240, dep_hour=17, carrier="UA"),
    ]
    ranked = rank_offers(offers, PreferenceWeights(), request_bos_jfk)
    by_id = {r.offer.id: r for r in ranked}
    assert by_id[offers[0].id].price_score == 1.0
    assert by_id[offers[2].id].price_score == 0.0
    assert by_id[offers[0].id].time_score == 1.0
    for r in ranked:
        for s in r.scores().values():
            assert 0.0 <= s <= 1.0
        # Explanation reflects actual component values.
        assert f"price {r.price_score:.2f}" in r.explanation


def test_ranking_is_deterministic(request_bos_jfk):
    offers = [
        make_offer(price="150", duration=90, dep_hour=h, carrier=c)
        for h, c in ((6, "DL"), (9, "AA"), (13, "UA"), (18, "B6"))
    ]
    w = PreferenceWeights()
    first = [r.offer.id for r in rank_offers(offers, w, request_bos_jfk)]
    second = [r.offer.id for r in rank_offers(list(reversed(offers)), w, request_bos_jfk)]
    assert first == second


def test_hard_constraint_nonstop(request_bos_jfk):
    request_bos_jfk.constraints.nonstop_only = True
    nonstop = make_offer(dep_hour=9)
    onestop = make_offer(dep_hour=10, connections=1, price="90")
    result = apply_hard_constraints([nonstop, onestop], request_bos_jfk)
    assert [o.id for o in result.kept] == [nonstop.id]
    assert any("stop" in r for r in result.rejected[onestop.id])


def test_hard_constraint_max_price_and_red_eye(request_bos_jfk):
    request_bos_jfk.constraints.max_price = 200
    request_bos_jfk.constraints.no_red_eye = True
    ok = make_offer(price="150", dep_hour=9)
    expensive = make_offer(price="250", dep_hour=10)
    red_eye = make_offer(price="100", dep_hour=23)
    result = apply_hard_constraints([ok, expensive, red_eye], request_bos_jfk)
    assert [o.id for o in result.kept] == [ok.id]
    assert expensive.id in result.rejected
    assert any("red-eye" in r for r in result.rejected[red_eye.id])


def test_hard_constraint_carry_on_and_arrive_by(request_bos_jfk):
    request_bos_jfk.constraints.carry_on_required = True
    request_bos_jfk.constraints.arrive_by = datetime(2026, 10, 9, 12, 0)
    ok = make_offer(dep_hour=8, carry_on=True)
    no_bag = make_offer(dep_hour=9, carry_on=False)
    too_late = make_offer(dep_hour=18, carry_on=True)
    result = apply_hard_constraints([ok, no_bag, too_late], request_bos_jfk)
    assert [o.id for o in result.kept] == [ok.id]


def test_time_window_filters_departures(request_bos_jfk):
    request_bos_jfk.outbound_window = TimeWindow(earliest=time(16, 0))
    early = make_offer(dep_hour=9)
    late = make_offer(dep_hour=18)
    result = apply_hard_constraints([early, late], request_bos_jfk)
    assert [o.id for o in result.kept] == [late.id]


def test_expired_offers_filtered(request_bos_jfk):
    live = make_offer(dep_hour=9, expires_at=NOW + timedelta(minutes=30))
    expired = make_offer(dep_hour=10, expires_at=NOW - timedelta(minutes=1))
    result = apply_hard_constraints([live, expired], request_bos_jfk, now=NOW)
    assert [o.id for o in result.kept] == [live.id]
    assert result.rejected[expired.id] == ["offer expired"]


def test_presentation_labels_and_dedupe(request_bos_jfk):
    balanced = make_offer(price="150", duration=90, dep_hour=10)
    cheapest = make_offer(price="60", duration=300, connections=1, dep_hour=7)
    fastest = make_offer(price="400", duration=65, dep_hour=11)
    ranked = rank_offers(
        [balanced, cheapest, fastest], PreferenceWeights(), request_bos_jfk
    )
    options = select_presentation(ranked)
    labels = {o.label: o.ranked.offer.id for o in options}
    assert "best" in labels and "cheapest" in labels and "fastest" in labels
    assert labels["cheapest"] == cheapest.id
    assert labels["fastest"] == fastest.id
    # No offer appears twice.
    ids = [o.ranked.offer.id for o in options]
    assert len(ids) == len(set(ids))


def test_presentation_collapses_duplicates(request_bos_jfk):
    # One offer that is simultaneously best, cheapest and fastest.
    only = make_offer(price="100", duration=80, dep_hour=10)
    slower = make_offer(price="180", duration=200, connections=1, dep_hour=14)
    ranked = rank_offers([only, slower], PreferenceWeights(), request_bos_jfk)
    options = select_presentation(ranked)
    ids = [o.ranked.offer.id for o in options]
    assert len(ids) == len(set(ids)) == 2
