from datetime import date, timedelta

import pytest

from src.models.travel_request import DateRange, PreferenceWeights, TravelRequest
from tests.conftest import NOW, make_offer


class TestWeightParsing:
    def test_full_override(self):
        w = PreferenceWeights.parse_overrides("price 60, time 25, convenience 15")
        assert (w.price, w.time, w.convenience, w.reliability) == (60, 25, 15, 0)

    def test_colon_and_equals_syntax(self):
        w = PreferenceWeights.parse_overrides("price=70 time:20")
        assert w.price == 70 and w.time == 20

    def test_partial_override_scales_rest_from_base(self):
        # price 80 leaves 20 to split across time/convenience/reliability
        # in base proportion 30:20:10.
        w = PreferenceWeights.parse_overrides("price 80")
        assert w.price == 80
        assert w.time == pytest.approx(10)
        assert w.convenience == pytest.approx(20 * 20 / 60)
        assert w.reliability == pytest.approx(20 * 10 / 60)

    def test_aliases(self):
        w = PreferenceWeights.parse_overrides("cost 50, speed 50")
        assert w.price == 50 and w.time == 50

    def test_no_components_raises(self):
        with pytest.raises(ValueError):
            PreferenceWeights.parse_overrides("make it nice")

    def test_normalized_sums_to_one(self):
        n = PreferenceWeights(price=60, time=25, convenience=15, reliability=0).normalized()
        assert sum(n.values()) == pytest.approx(1.0)


class TestTravelRequest:
    def test_missing_required_fields(self):
        r = TravelRequest(origin="BOS")
        assert r.missing_required_fields() == ["destination", "outbound_date"]
        r = TravelRequest(
            origin="BOS", destination="JFK",
            outbound_date=DateRange(start=date(2026, 10, 9)),
        )
        assert r.missing_required_fields() == []

    def test_date_range_defaults_and_validation(self):
        r = DateRange(start=date(2026, 10, 9))
        assert r.end == r.start
        with pytest.raises(ValueError):
            DateRange(start=date(2026, 10, 9), end=date(2026, 10, 8))

    def test_effective_constraints_merges_shortcuts(self):
        r = TravelRequest(max_price=300, max_stops=1)
        c = r.effective_constraints()
        assert c.max_price == 300 and c.max_stops == 1


class TestOffer:
    def test_expiration(self):
        o = make_offer(expires_at=NOW + timedelta(minutes=5))
        assert not o.is_expired(NOW)
        assert o.is_expired(NOW + timedelta(minutes=5))
        assert not make_offer(expires_at=None).is_expired(NOW)

    def test_red_eye(self):
        assert make_offer(dep_hour=23).is_red_eye()
        assert make_offer(dep_hour=3).is_red_eye()
        assert not make_offer(dep_hour=10).is_red_eye()

    def test_fingerprint_stable_across_reprice(self):
        a = make_offer(price="100")
        b = a.model_copy(deep=True)
        b.provider_offer_id = "different"
        b.total_price = b.total_price + 50
        assert a.itinerary_fingerprint() == b.itinerary_fingerprint()

    def test_fingerprint_changes_with_schedule(self):
        a = make_offer(dep_hour=10)
        b = make_offer(dep_hour=12)
        assert a.itinerary_fingerprint() != b.itinerary_fingerprint()

    def test_essentially_same(self):
        a = make_offer(price="100", dep_hour=10)
        b = a.model_copy(deep=True)
        b.provider_offer_id = "x"
        b.total_price = b.total_price + 3  # within 5%
        assert a.is_essentially_same_as(b)
        c = make_offer(price="100", dep_hour=15)
        assert not a.is_essentially_same_as(c)
