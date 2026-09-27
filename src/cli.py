"""Deterministic tool surface for the travel agent.

The OpenClaw skill invokes these commands; the LLM handles language and
orchestration while this CLI (and the services beneath it) owns state,
constraints, ranking, persistence, and the SerpAPI search budget. Every
command prints a single JSON object on stdout (and mirrors it to
``data/last_response.json`` for exec environments that swallow stdout).

Reliability contract: every response carries ``command``, ``argv`` and
``generated_at``. Failures of any kind (usage errors, illegal state
changes, unexpected exceptions) still produce ``{"ok": false, ...}`` JSON,
and a command that dies before answering leaves a ``status: incomplete``
record in the fallback file, never the previous command's output.

Payment safety: this agent never purchases tickets and never collects
card or passport data. ``link`` hands the user a Google Flights booking
URL; the user buys on the airline site and reports back with ``booked``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from src.config import Settings, build_providers
from src.models.booking import PassengerIdentity, Trip, TripState
from src.models.preferences import UserPreferences
from src.models.travel_request import TravelRequest
from src.services import state as sm
from src.services.handoff import HandoffService
from src.services.monitoring import MonitoringService
from src.services.price_watch import PriceWatchService, eligible_offers
from src.services.search import SearchService, format_options, is_demo_outcome
from src.services.search_budget import BudgetConfig, SearchBudgetManager
from src.storage.repository import SQLiteRepository


#: The command being run, stamped onto every response (set by main()).
_CONTEXT: dict[str, Any] = {"command": None, "argv": []}


def _stamp(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "command": _CONTEXT["command"],
        "argv": _CONTEXT["argv"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _persist(text: str) -> None:
    # Belt-and-braces for exec environments that swallow stdout (seen with
    # sandboxed agent shells): every response is also written to a file the
    # caller can `cat` afterwards.
    try:
        out = Path(os.environ.get("TRAVEL_AGENT_HOME", ".")) / "data"
        out.mkdir(parents=True, exist_ok=True)
        tmp = out / f"last_response.json.{os.getpid()}.tmp"
        tmp.write_text(text)
        tmp.replace(out / "last_response.json")
    except OSError:
        pass


def _out(payload: dict[str, Any]) -> None:
    text = json.dumps(_stamp(payload), default=str, indent=2)
    print(text)
    _persist(text)


def _fail(message: str, exit_code: int = 1, **extra: Any) -> None:
    _out({"ok": False, "error": message, **extra})
    sys.exit(exit_code)


def _begin(argv: list[str]) -> None:
    """Record the command and replace the fallback file with an
    'incomplete' marker, so a crash can never leave the previous command's
    result looking like this command's answer."""
    _CONTEXT["command"] = argv[0] if argv else None
    _CONTEXT["argv"] = list(argv)
    _persist(json.dumps(_stamp({
        "ok": False,
        "status": "incomplete",
        "error": "The command started but did not finish, so no result was "
                 "recorded. Do not reuse earlier results.",
    }), indent=2))


class CLIUsageError(SystemExit):
    """Bad command-line usage. Still a SystemExit (code 2) for callers that
    expect argparse semantics, but carries the message so main() can report
    it as JSON."""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        raise CLIUsageError(f"{self.prog}: {message}")


def _load_json_arg(value: str) -> dict:
    if value == "-":
        value = sys.stdin.read()
    return json.loads(value)


class App:
    def __init__(self) -> None:
        self.settings = Settings.from_env()
        self.repo = SQLiteRepository(self.settings.database_path)
        self.budget = SearchBudgetManager(
            self.repo,
            BudgetConfig(
                monthly_limit=self.settings.serpapi_monthly_limit,
                reserve=self.settings.serpapi_reserve,
            ),
        )
        self.providers = build_providers(
            self.settings, repo=self.repo, budget=self.budget
        )
        self.search = SearchService(self.providers, self.repo)
        self.monitoring = MonitoringService(self.providers, self.repo)
        self.watches = PriceWatchService(self.providers, self.repo, self.budget)
        self.handoff = HandoffService(self.repo)

    def _get_trip(self, trip_id: str) -> Trip:
        trip = self.repo.get_trip(trip_id)
        if trip is None:
            _fail(f"Unknown trip {trip_id}")
        return trip


def cmd_new_trip(app: App, args: argparse.Namespace) -> None:
    try:
        payload = _load_json_arg(args.request_json)
        prefs = app.repo.get_preferences(args.user)
        if prefs:
            if "origin" not in payload and prefs.home_airport:
                payload["origin"] = prefs.home_airport
            payload.setdefault("cabin", prefs.cabin_preference.value)
        request = TravelRequest.model_validate(payload)
    except (ValidationError, json.JSONDecodeError) as exc:
        _fail(f"Invalid travel request: {exc}")
    trip = Trip(user_id=args.user, request=request)
    app.repo.save_trip(trip)
    missing = request.missing_required_fields()
    target = TripState.NEEDS_INFORMATION if missing else TripState.READY_TO_SEARCH
    sm.transition(app.repo, trip, target)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "missing_fields": missing,
        }
    )


def cmd_update_trip(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    try:
        updates = _load_json_arg(args.request_json)
        merged = trip.request.model_dump(mode="json")
        merged.update(updates)
        trip.request = TravelRequest.model_validate(merged)
    except (ValidationError, json.JSONDecodeError) as exc:
        _fail(f"Invalid update: {exc}")
    app.repo.save_trip(trip)
    missing = trip.request.missing_required_fields()
    if trip.state in (TripState.DRAFT, TripState.NEEDS_INFORMATION):
        target = TripState.NEEDS_INFORMATION if missing else TripState.READY_TO_SEARCH
        sm.transition(app.repo, trip, target)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "missing_fields": missing,
        }
    )


def cmd_search(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    if trip.request.missing_required_fields():
        _fail(
            "Trip is missing required fields",
            missing_fields=trip.request.missing_required_fields(),
        )
    try:
        outcome = app.search.search(trip)
    except sm.InvalidTransition as exc:
        _fail(f"Cannot search this trip now: {exc}", state=trip.state.value)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "display_text": format_options(outcome, app.repo),
            "demo_data": is_demo_outcome(outcome),
            "options": [
                {
                    "index": i + 1,
                    "label": opt.label,
                    "offer_id": opt.ranked.offer.id,
                    "provider": opt.ranked.offer.provider,
                    "utility": opt.ranked.utility,
                    "scores": opt.ranked.scores(),
                    "explanation": opt.ranked.explanation,
                }
                for i, opt in enumerate(outcome.options)
            ],
            "total_offers": outcome.total_offers,
            "filtered_out": outcome.filtered_out,
            "rejection_reasons": outcome.rejection_summary,
            "provider_errors": outcome.provider_errors,
        }
    )


def cmd_refine(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    try:
        outcome = app.search.refine(trip, args.command)
    except sm.InvalidTransition as exc:
        _fail(f"Cannot refine this trip now: {exc}", state=trip.state.value)
    except ValueError as exc:
        _fail(str(exc))
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "display_text": format_options(outcome, app.repo),
            "demo_data": is_demo_outcome(outcome),
        }
    )


def cmd_select(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    try:
        offer = app.search.select_option(trip, args.option)
    except (ValueError, sm.InvalidTransition) as exc:
        _fail(str(exc))
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "selected_offer_id": offer.id,
        }
    )


def cmd_status(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    payload = {
        "ok": True,
        "trip_id": trip.id,
        "state": trip.state.value,
        "selected_offer_id": trip.selected_offer_id,
        "booking_id": trip.booking_id,
        "missing_fields": trip.request.missing_required_fields(),
    }
    if trip.booking_id:
        booking = app.repo.get_booking(trip.booking_id)
        if booking:
            payload["booking_status"] = booking.status.value
            payload["booking_reference"] = booking.booking_reference
    _out(payload)


def cmd_cancel_trip(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    try:
        sm.transition(app.repo, trip, TripState.CANCELLED)
    except sm.InvalidTransition as exc:
        _fail(str(exc))
    _out({"ok": True, "trip_id": trip.id, "state": trip.state.value})


def cmd_traveler_add(app: App, args: argparse.Namespace) -> None:
    try:
        data = _load_json_arg(args.json)
        data["user_id"] = args.user
        traveler = PassengerIdentity.model_validate(data)
    except (ValidationError, json.JSONDecodeError) as exc:
        _fail(f"Invalid traveler: {exc}")
    app.repo.save_traveler(traveler)
    _out({"ok": True, "traveler_id": traveler.id})


def cmd_prefs_get(app: App, args: argparse.Namespace) -> None:
    prefs = app.repo.get_preferences(args.user) or UserPreferences(user_id=args.user)
    _out({"ok": True, "preferences": prefs.model_dump(mode="json")})


def cmd_prefs_set(app: App, args: argparse.Namespace) -> None:
    try:
        updates = _load_json_arg(args.json)
        current = app.repo.get_preferences(args.user) or UserPreferences(
            user_id=args.user
        )
        merged = current.model_dump(mode="json")
        merged.update(updates)
        merged["user_id"] = args.user
        prefs = UserPreferences.model_validate(merged)
    except (ValidationError, json.JSONDecodeError) as exc:
        _fail(f"Invalid preferences: {exc}")
    app.repo.save_preferences(prefs)
    _out({"ok": True, "preferences": prefs.model_dump(mode="json")})


def cmd_monitor_run(app: App, args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    changes = app.monitoring.run_due(now)
    watch_notes = app.watches.run_due(now)
    # Everything that should reach the user, reminders included. Nothing is
    # delivered by this command: the scheduler/agent relays display_text.
    notifications = app.monitoring.outbox + app.watches.outbox
    _out(
        {
            "ok": True,
            "notification_count": len(notifications),
            "notifications": notifications,
            "display_text": "\n\n".join(n["text"] for n in notifications),
            "changes": [c.model_dump(mode="json") for c in changes],
            "watch_notifications": watch_notes,
        }
    )


# -- link handoff / external booking ----------------------------------------

def cmd_link(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    try:
        result = app.handoff.booking_link(trip)
    except (ValueError, sm.InvalidTransition) as exc:
        _fail(str(exc), state=trip.state.value)
    _out({"ok": True, **result.model_dump(mode="json")})


def cmd_booked(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    existing = app.repo.get_booked_trip(trip.booking_id) if trip.booking_id else None
    if existing is not None:
        # Idempotent: saying "booked" twice must not create a second trip.
        _out(
            {
                "ok": True,
                "already_booked": True,
                "trip_id": trip.id,
                "state": trip.state.value,
                "booked_trip": existing.model_dump(mode="json"),
            }
        )
        return
    try:
        details = _load_json_arg(args.details) if args.details else {}
    except json.JSONDecodeError as exc:
        _fail(f"Invalid --details JSON: {exc}")
    if not isinstance(details, dict):
        _fail("--details must be a JSON object")
    try:
        if args.option:
            # Booked straight from the search results: record which one.
            app.search.select_option(trip, args.option)
        booked = app.handoff.mark_booked(trip, details)
    except (ValueError, sm.InvalidTransition) as exc:
        _fail(str(exc), state=trip.state.value)
    app.monitoring.create_tasks_for_trip(booked)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "booked_trip": booked.model_dump(mode="json"),
        }
    )


# -- price watches -----------------------------------------------------------

def cmd_track(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    # A named option tracks that exact flight. Otherwise the watch tracks
    # the cheapest fare meeting the trip's requirements, and its initial
    # price is measured the same way (no API call either way).
    initial = None
    offer = None
    itinerary_id: Optional[str] = None
    if args.option:
        token = args.option.strip()
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(trip.presented_offer_ids):
                offer = app.repo.get_offer(trip.presented_offer_ids[idx])
        else:
            offer = app.repo.get_offer(token)
        if offer is None:
            _fail(
                f"Option {token} does not exist; "
                f"{len(trip.presented_offer_ids)} option(s) were presented"
            )
        itinerary_id = offer.itinerary_fingerprint()
    else:
        eligible = eligible_offers(
            trip.request, app.repo.get_offers_for_trip(trip.id)
        )
        offer = min(eligible, key=lambda o: o.total_price) if eligible else None
    if offer is not None:
        initial = float(offer.total_price)
    watch = app.watches.create_watch(
        trip.user_id,
        trip.request,
        trip_id=trip.id,
        target_price=args.target,
        initial_price=initial,
        itinerary_id=itinerary_id,
    )
    if trip.state in (TripState.OPTIONS_READY, TripState.OPTION_SELECTED,
                      TripState.AWAITING_USER_BOOKING):
        sm.transition(app.repo, trip, TripState.PRICE_WATCH_ACTIVE)
    _out(
        {
            "ok": True,
            "watch_id": watch.id,
            "tracking": "selected flight" if itinerary_id
            else "cheapest fare matching the trip's requirements",
            "trip_state": trip.state.value,
            "initial_price": watch.initial_price,
            "target_price": watch.target_price,
            "next_check_at": watch.next_check_at.isoformat()
            if watch.next_check_at else None,
        }
    )


def cmd_untrack(app: App, args: argparse.Namespace) -> None:
    watch = (
        app.repo.get_watch(args.watch)
        or app.watches.find_watch(args.user, args.watch)
        if args.user
        else app.repo.get_watch(args.watch)
    )
    if watch is None:
        _fail(f"No matching active watch for {args.watch!r}")
    app.watches.stop_watch(watch.id)
    _out({"ok": True, "watch_id": watch.id, "active": False})


def cmd_watches(app: App, args: argparse.Namespace) -> None:
    watches = app.repo.list_watches(args.user, active_only=not args.all)
    _out(
        {
            "ok": True,
            "watches": [
                {
                    "id": w.id,
                    "route": f"{w.origin}->{w.destination}",
                    "outbound": w.request.outbound_date.start.isoformat()
                    if w.request.outbound_date else None,
                    "latest_price": w.latest_price,
                    "lowest_price": w.lowest_price,
                    "initial_price": w.initial_price,
                    "target_price": w.target_price,
                    "active": w.active,
                    "next_check_at": w.next_check_at.isoformat()
                    if w.next_check_at else None,
                }
                for w in watches
            ],
        }
    )


def cmd_trips(app: App, args: argparse.Namespace) -> None:
    trips = app.repo.list_booked_trips(args.user)
    _out(
        {
            "ok": True,
            "trips": [t.model_dump(mode="json") for t in trips],
        }
    )


def cmd_usage(app: App, args: argparse.Namespace) -> None:
    _out({"ok": True, "usage": app.budget.get_usage().model_dump(mode="json")})


def cmd_plan(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    plan = app.budget.estimate_search_cost(
        trip.request,
        flex_outbound_days=args.flex_out,
        flex_return_days=args.flex_return,
    )
    _out({"ok": True, "plan": plan.model_dump(mode="json")})


def cmd_chart(app: App, args: argparse.Namespace) -> None:
    from src.services.charts import render_price_chart

    watch = app.repo.get_watch(args.watch) if args.watch else None
    if watch is None and args.user and args.route:
        watch = app.watches.find_watch(args.user, args.route)
    if watch is None and args.user:
        active = app.repo.list_watches(args.user, active_only=True)
        watch = active[0] if active else None
    if watch is None:
        _fail("No matching price watch found")
    observations = app.repo.observations_for_watch(watch.id)
    if not observations:
        _fail("No price observations recorded yet for this watch")
    out = args.out or f"data/charts/{watch.id}.png"
    path = render_price_chart(watch, observations, out)
    _out(
        {
            "ok": True,
            "watch_id": watch.id,
            "chart_path": path,
            "observations": len(observations),
        }
    )


def cmd_dashboard(app: App, args: argparse.Namespace) -> None:
    from src.services.dashboard import serve

    serve(app.repo, app.budget, port=args.port, host=args.host)


def cmd_checkpoint(app: App, args: argparse.Namespace) -> None:
    from src.services.planning import PlanCheckpoint, PlanningStore
    trip = app._get_trip(args.trip)
    store = PlanningStore(app.repo)
    if args.json is not None:
        try:
            store.save(trip.id, PlanCheckpoint.model_validate(_load_json_arg(args.json)))
        except (ValidationError, ValueError) as exc:
            _fail(str(exc))
    checkpoint = store.get(trip.id)
    _out({"ok": True, "trip_id": trip.id,
          "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
          "events": store.events(trip.id)})


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="travel-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *specs):
        sp = sub.add_parser(name)
        for flags, kwargs in specs:
            sp.add_argument(flags, **kwargs)
        sp.set_defaults(fn=fn)
        return sp

    add("checkpoint", cmd_checkpoint,
        ("--trip", {"required": True}),
        ("--json", {"default": None, "help": "Validated checkpoint JSON or stdin -"}))
    add("new-trip", cmd_new_trip,
        ("--user", {"required": True}),
        ("--request-json", {"required": True, "help": "JSON or '-' for stdin"}))
    add("update-trip", cmd_update_trip,
        ("--trip", {"required": True}),
        ("--request-json", {"required": True}))
    add("search", cmd_search, ("--trip", {"required": True}))
    add("refine", cmd_refine,
        ("--trip", {"required": True}),
        ("--command", {"required": True}))
    add("select", cmd_select,
        ("--trip", {"required": True}),
        ("--option", {"required": True, "help": "1-based index or offer id"}))
    add("status", cmd_status, ("--trip", {"required": True}))
    add("cancel-trip", cmd_cancel_trip, ("--trip", {"required": True}))
    add("traveler-add", cmd_traveler_add,
        ("--user", {"required": True}),
        ("--json", {"required": True}))
    add("prefs-get", cmd_prefs_get, ("--user", {"required": True}))
    add("prefs-set", cmd_prefs_set,
        ("--user", {"required": True}),
        ("--json", {"required": True}))
    add("monitor-run", cmd_monitor_run)
    # Link-handoff flow (no in-agent payment)
    add("link", cmd_link, ("--trip", {"required": True}))
    add("booked", cmd_booked,
        ("--trip", {"required": True}),
        ("--option", {"default": None,
                      "help": "presented option number that was booked "
                              "(when booked straight from the search results)"}),
        ("--details", {"default": None,
                       "help": "JSON: confirmation_code, flight_number, ..."}))
    # Price watches / budget / charts / dashboard
    add("track", cmd_track,
        ("--trip", {"required": True}),
        ("--option", {"default": None, "help": "presented option number"}),
        ("--target", {"type": float, "default": None}))
    add("untrack", cmd_untrack,
        ("--watch", {"required": True, "help": "watch id or airport code"}),
        ("--user", {"default": None}))
    add("watches", cmd_watches,
        ("--user", {"required": True}),
        ("--all", {"action": "store_true"}))
    add("trips", cmd_trips, ("--user", {"required": True}))
    add("usage", cmd_usage)
    add("plan", cmd_plan,
        ("--trip", {"required": True}),
        ("--flex-out", {"type": int, "default": 0}),
        ("--flex-return", {"type": int, "default": 0}))
    add("chart", cmd_chart,
        ("--watch", {"default": None}),
        ("--user", {"default": None}),
        ("--route", {"default": None, "help": "airport code, e.g. LAX"}),
        ("--out", {"default": None}))
    add("dashboard", cmd_dashboard,
        ("--port", {"type": int, "default": 8090}),
        ("--host", {"default": "127.0.0.1",
                    "help": "bind address; the page shows confirmation codes, "
                            "so expose it (0.0.0.0) only behind auth"}))
    return p


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    _begin(argv)
    try:
        args = build_parser().parse_args(argv)
    except CLIUsageError as exc:
        _fail(f"Invalid command usage: {exc.message}", exit_code=2,
              hint="Run `python3 -m src.cli <command> --help` for the exact flags.")
    except SystemExit as exc:
        if exc.code in (0, None):  # --help printed usage to stdout
            _persist(json.dumps(_stamp({"ok": True, "status": "help printed"})))
        raise
    app: Optional[App] = None
    try:
        app = App()
        args.fn(app, args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last line of defence
        traceback.print_exc(file=sys.stderr)
        _fail(f"Unexpected error in {_CONTEXT['command']}: "
              f"{type(exc).__name__}: {exc}")
    finally:
        if app is not None:
            app.repo.close()


if __name__ == "__main__":
    main()
