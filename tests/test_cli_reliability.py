"""CLI-level regression tests: the surface the agent actually calls.

Each test drives ``src.cli.main`` in-process exactly as the skill does
(one command per call) against a temporary database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.cli import main
from src.providers.mock import DEMO_NOTICE

REQUEST = json.dumps({
    "origin": "BOS", "destination": "ORD",
    "outbound_date": {"start": "2027-10-09"},
})


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.sqlite3"))
    monkeypatch.setenv("TRAVEL_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("TRAVEL_PROVIDERS", "mock")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    def run(*argv):
        """Returns (exit_code, parsed stdout JSON)."""
        code = 0
        try:
            main(list(argv))
        except SystemExit as exc:
            code = exc.code
        return code, json.loads(capsys.readouterr().out)

    run.fallback = lambda: json.loads(
        (tmp_path / "data" / "last_response.json").read_text()
    )
    return run


def new_searched_trip(cli):
    _, created = cli("new-trip", "--user", "u1", "--request-json", REQUEST)
    trip = created["trip_id"]
    code, searched = cli("search", "--trip", trip)
    assert code == 0 and searched["options"]
    return trip


# -- 1. no silent fake fares -------------------------------------------------

def test_unset_provider_fails_closed_instead_of_serving_mock(cli, monkeypatch):
    monkeypatch.delenv("TRAVEL_PROVIDERS")
    _, created = cli("new-trip", "--user", "u1", "--request-json", REQUEST)
    code, result = cli("search", "--trip", created["trip_id"])
    assert code == 0
    assert result["state"] == "SEARCH_FAILED"
    assert result["total_offers"] == 0 and result["options"] == []
    assert "SERPAPI_API_KEY" in result["display_text"]


def test_mock_results_are_labelled_as_demo_data(cli):
    _, created = cli("new-trip", "--user", "u1", "--request-json", REQUEST)
    _, result = cli("search", "--trip", created["trip_id"])
    assert result["demo_data"] is True
    assert result["display_text"].startswith(DEMO_NOTICE)
    assert result["display_text"].endswith(DEMO_NOTICE)
    assert {o["provider"] for o in result["options"]} == {"mock"}
    _, refined = cli("refine", "--trip", created["trip_id"], "--command", "cheaper")
    assert refined["demo_data"] is True and DEMO_NOTICE in refined["display_text"]


# -- 2. crashes never leave a stale answer -----------------------------------

def test_search_after_selecting_an_option_searches_again(cli):
    trip = new_searched_trip(cli)
    cli("select", "--trip", trip, "--option", "1")
    code, result = cli("search", "--trip", trip)
    assert code == 0 and result["ok"] is True
    assert result["state"] == "OPTIONS_READY"


def test_refusals_are_json_not_tracebacks(cli):
    trip = new_searched_trip(cli)
    cli("booked", "--trip", trip, "--option", "1")
    code, result = cli("search", "--trip", trip)  # already booked
    assert code == 1 and result["ok"] is False
    assert "Cannot search" in result["error"]
    code, result = cli("refine", "--trip", trip, "--command", "cheaper")
    assert code == 1 and "Cannot refine" in result["error"]


def test_unexpected_error_is_reported_as_json_and_replaces_fallback(cli, monkeypatch):
    trip = new_searched_trip(cli)

    def boom(self, *a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("src.services.search.SearchService.search", boom)
    code, result = cli("search", "--trip", trip)
    assert code == 1 and result["ok"] is False
    assert "RuntimeError: provider exploded" in result["error"]
    assert cli.fallback() == result  # not the previous search's output


def test_usage_errors_are_json(cli):
    code, result = cli("search")  # missing --trip
    assert code == 2 and result["ok"] is False
    assert "--trip" in result["error"]
    assert cli.fallback()["command"] == "search"


def test_hard_crash_leaves_incomplete_marker_not_previous_result(cli, monkeypatch):
    trip = new_searched_trip(cli)  # fallback now holds a successful search

    def interrupted(self):
        raise KeyboardInterrupt  # bypasses every `except Exception`

    monkeypatch.setattr("src.cli.App.__init__", interrupted)
    with pytest.raises(KeyboardInterrupt):
        main(["search", "--trip", trip])
    marker = cli.fallback()
    assert marker["status"] == "incomplete" and marker["ok"] is False
    assert marker["argv"] == ["search", "--trip", trip]


def test_every_response_is_stamped(cli):
    code, result = cli("usage")
    assert code == 0
    assert result["command"] == "usage" and result["argv"] == ["usage"]
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None


# -- 3. booking without orphans ----------------------------------------------

def test_booked_without_a_pick_saves_nothing(cli):
    trip = new_searched_trip(cli)
    code, result = cli("booked", "--trip", trip)
    assert code == 1 and "--option" in result["error"]
    assert cli("trips", "--user", "u1")[1]["trips"] == []
    assert cli("status", "--trip", trip)[1]["state"] == "OPTIONS_READY"


def test_booked_straight_from_search_results(cli):
    trip = new_searched_trip(cli)
    code, result = cli("booked", "--trip", trip, "--option", "1",
                       "--details", '{"confirmation_code":"ABC123"}')
    assert code == 0 and result["state"] == "MONITORING"
    assert result["booked_trip"]["confirmation_code"] == "ABC123"
    # Saying it twice is idempotent.
    code, again = cli("booked", "--trip", trip)
    assert code == 0 and again["already_booked"] is True
    assert len(cli("trips", "--user", "u1")[1]["trips"]) == 1


def test_booked_after_link_unavailable(cli):
    trip = new_searched_trip(cli)
    cli("select", "--trip", trip, "--option", "2")
    _, link = cli("link", "--trip", trip)  # mock offers carry no URL
    assert link["state"] == "BOOKING_LINK_UNAVAILABLE"
    assert DEMO_NOTICE in link["display_text"]
    code, result = cli("booked", "--trip", trip)
    assert code == 0 and result["state"] == "MONITORING"


def test_booked_with_bad_details_saves_nothing(cli):
    trip = new_searched_trip(cli)
    code, result = cli("booked", "--trip", trip, "--option", "1",
                       "--details", "{not json")
    assert code == 1 and "details" in result["error"]
    assert cli("trips", "--user", "u1")[1]["trips"] == []


# -- 4. reminders are returned by monitor-run --------------------------------

def test_monitor_run_returns_due_reminders(cli):
    trip = new_searched_trip(cli)
    # Departure 10h from now, Boston local time: the 24h departure and
    # check-in reminders are due, the 3h one is not.
    local = datetime.now(ZoneInfo("America/New_York")) + timedelta(hours=10)
    details = json.dumps({"departure": local.replace(tzinfo=None).isoformat(),
                          "origin": "BOS", "confirmation_code": "XYZ789"})
    cli("booked", "--trip", trip, "--option", "1", "--details", details)
    code, result = cli("monitor-run")
    assert code == 0
    assert result["notification_count"] == 2
    kinds = sorted(n["kind"] for n in result["notifications"])
    assert kinds == ["check_in_reminder", "departure_reminder"]
    assert all(n["user_id"] == "u1" for n in result["notifications"])
    assert "XYZ789" in result["display_text"]
    # Already sent: the next run is quiet.
    assert cli("monitor-run")[1]["notification_count"] == 0


# -- 5. tracking semantics ---------------------------------------------------

def test_track_named_option_tracks_that_flight(cli):
    trip = new_searched_trip(cli)
    code, result = cli("track", "--trip", trip, "--option", "1", "--target", "100")
    assert code == 0 and result["tracking"] == "selected flight"
    code, result = cli("track", "--trip", trip, "--option", "99")
    assert code == 1 and "does not exist" in result["error"]
