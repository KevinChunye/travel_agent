from src.models.booking import TripState
from src.services.search import format_options


def test_search_presents_up_to_three_distinct_options(repo, search_service, trip):
    outcome = search_service.search(trip)
    assert trip.state == TripState.OPTIONS_READY
    assert 1 <= len(outcome.options) <= 3
    ids = [o.ranked.offer.id for o in outcome.options]
    assert len(ids) == len(set(ids))
    assert outcome.options[0].label == "best"
    # Presented options are persisted on the trip for later selection.
    assert repo.get_trip(trip.id).presented_offer_ids == ids


def test_format_options_contains_required_fields(repo, search_service, trip):
    outcome = search_service.search(trip)
    text = format_options(outcome, repo)
    offer = outcome.options[0].ranked.offer
    assert str(offer.total_price) in text
    assert offer.currency in text
    assert "why:" in text
    assert "nonstop" in text or "stop(s)" in text


def test_refine_nonstop_only(repo, search_service, trip):
    search_service.search(trip)
    outcome = search_service.refine(trip, "nonstop only")
    assert all(o.ranked.offer.connections == 0 for o in outcome.options)
    assert trip.state == TripState.OPTIONS_READY


def test_refine_cheaper_reweights(repo, search_service, trip):
    first = search_service.search(trip)
    outcome = search_service.refine(trip, "cheaper")
    assert trip.request.weights.price == 60
    cheapest_shown = min(
        o.ranked.offer.total_price for o in first.options
    )
    assert outcome.options[0].ranked.offer.total_price <= cheapest_shown * 2


def test_refine_explicit_weights(repo, search_service, trip):
    search_service.search(trip)
    search_service.refine(trip, "price 60, time 25, convenience 15")
    w = trip.request.weights
    assert (w.price, w.time, w.convenience, w.reliability) == (60, 25, 15, 0)


def test_refine_unknown_command_raises(repo, search_service, trip):
    search_service.search(trip)
    try:
        search_service.refine(trip, "teleport me")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_select_by_index_records_feedback(repo, search_service, trip):
    search_service.search(trip)
    offer = search_service.select_option(trip, "2")
    assert trip.selected_offer_id == offer.id
    assert trip.state == TripState.OPTION_SELECTED
    row = repo._execute(
        "SELECT COUNT(*) FROM feedback WHERE trip_id=?", (trip.id,)
    ).fetchone()
    assert row[0] == 1


def test_select_invalid_index(repo, search_service, trip):
    search_service.search(trip)
    try:
        search_service.select_option(trip, "9")
        assert False, "expected ValueError"
    except ValueError:
        pass
