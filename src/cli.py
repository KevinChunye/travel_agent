"""Deterministic tool surface for the travel agent.

The OpenClaw skill invokes these commands; the LLM handles language and
orchestration while this CLI (and the services beneath it) owns state,
constraints, ranking, and booking authorization. Every command prints a
single JSON object on stdout.

Booking safety: ``confirm`` requires the literal flag ``--user-confirmed yes``
which the skill may only pass after the user explicitly approved the
displayed price — and even then, ``book`` re-validates price/itinerary/
passenger against the persisted intent, so a prompt-injected or confused
model cannot buy anything the user did not approve.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from src.config import Settings, build_providers
from src.models.booking import PassengerIdentity, PaymentToken, Trip, TripState
from src.models.preferences import UserPreferences
from src.models.travel_request import TravelRequest
from src.services import state as sm
from src.services.booking import (
    BookingAuthorizationError,
    BookingService,
    format_confirmation_prompt,
)
from src.services.handoff import HandoffService
from src.services.monitoring import MonitoringService
from src.services.price_watch import PriceWatchService
from src.services.search import SearchService, format_options
from src.services.search_budget import BudgetConfig, SearchBudgetManager
from src.storage.repository import SQLiteRepository


def _out(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str, indent=2))


def _fail(message: str, **extra: Any) -> None:
    _out({"ok": False, "error": message, **extra})
    sys.exit(1)


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
        self.booking = BookingService(self.providers, self.repo)
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
        request = TravelRequest.model_validate(_load_json_arg(args.request_json))
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
    outcome = app.search.search(trip)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "display_text": format_options(outcome, app.repo),
            "options": [
                {
                    "index": i + 1,
                    "label": opt.label,
                    "offer_id": opt.ranked.offer.id,
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
    except ValueError as exc:
        _fail(str(exc))
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "display_text": format_options(outcome, app.repo),
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


def _passenger_for(app: App, user_id: str, traveler_id: str | None) -> PassengerIdentity:
    travelers = app.repo.get_travelers(user_id)
    if traveler_id:
        for t in travelers:
            if t.id == traveler_id:
                return t
        _fail(f"Unknown traveler {traveler_id}")
    if not travelers:
        _fail(
            "No traveler profile on file. Add one with the traveler-add "
            "command before booking."
        )
    return travelers[0]


def cmd_reprice(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    passenger = _passenger_for(app, trip.user_id, args.traveler)
    try:
        intent, offer = app.booking.reprice_and_create_intent(
            trip, passenger, price_buffer_pct=app.settings.price_buffer_pct
        )
    except (BookingAuthorizationError, sm.InvalidTransition) as exc:
        _fail(str(exc), state=trip.state.value)
    except Exception as exc:  # OfferNotAvailableError et al.
        _fail(str(exc), state=app._get_trip(args.trip).state.value)
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "state": trip.state.value,
            "intent_id": intent.id,
            "total_price": str(offer.total_price),
            "currency": offer.currency,
            "display_text": format_confirmation_prompt(intent, offer, passenger),
        }
    )


def cmd_confirm(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    if args.user_confirmed != "yes":
        _fail("Refusing: --user-confirmed must be exactly 'yes'")
    try:
        intent = app.booking.confirm_intent(args.intent, trip.user_id)
    except BookingAuthorizationError as exc:
        _fail(str(exc))
    _out(
        {
            "ok": True,
            "trip_id": trip.id,
            "intent_id": intent.id,
            "confirmed_at": intent.confirmed_at.isoformat(),
        }
    )


def cmd_book(app: App, args: argparse.Namespace) -> None:
    trip = app._get_trip(args.trip)
    passenger = _passenger_for(app, trip.user_id, args.traveler)
    payment = PaymentToken(
        provider="duffel" if "duffel" in app.settings.providers else "mock",
        token=args.payment_token or "balance",
        kind=args.payment_kind,
    )
    try:
        decision = app.booking.execute_booking(trip, args.intent, passenger, payment)
    except (BookingAuthorizationError, sm.InvalidTransition) as exc:
        _fail(str(exc), state=trip.state.value)
    payload: dict[str, Any] = {
        "ok": decision.booked,
        "trip_id": trip.id,
        "state": trip.state.value,
        "booked": decision.booked,
    }
    if decision.booked:
        booking = app.repo.get_booking(decision.booking_id)
        app.monitoring.create_tasks_for_booking(booking)
        sm.transition(app.repo, trip, TripState.MONITORING)
        payload.update(
            {
                "state": trip.state.value,
                "booking_id": decision.booking_id,
                "booking_reference": decision.booking_reference,
            }
        )
    else:
        payload.update(
            {
                "rejection": decision.rejection.value if decision.rejection else None,
                "requires_reconfirmation": decision.requires_reconfirmation,
                "detail": decision.detail,
            }
        )
    _out(payload)


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
    notifications = app.watches.run_due(now)
    _out(
        {
            "ok": True,
            "changes": [c.model_dump(mode="json") for c in changes],
            "watch_notifications": notifications,
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
    details = _load_json_arg(args.details) if args.details else {}
    try:
        booked = app.handoff.mark_booked(trip, details)
    except sm.InvalidTransition as exc:
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
    # Initial price from the named presented option, else the cheapest
    # stored offer (no selection flow, no API call).
    initial = None
    offer = None
    if args.option and args.option.isdigit():
        idx = int(args.option) - 1
        if 0 <= idx < len(trip.presented_offer_ids):
            offer = app.repo.get_offer(trip.presented_offer_ids[idx])
    if offer is None:
        offers = app.repo.get_offers_for_trip(trip.id)
        offer = min(offers, key=lambda o: o.total_price) if offers else None
    if offer is not None:
        initial = float(offer.total_price)
    watch = app.watches.create_watch(
        trip.user_id,
        trip.request,
        trip_id=trip.id,
        target_price=args.target,
        initial_price=initial,
    )
    if trip.state in (TripState.OPTIONS_READY, TripState.OPTION_SELECTED,
                      TripState.AWAITING_USER_BOOKING):
        sm.transition(app.repo, trip, TripState.PRICE_WATCH_ACTIVE)
    _out(
        {
            "ok": True,
            "watch_id": watch.id,
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

    serve(app.repo, app.budget, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="travel-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *specs):
        sp = sub.add_parser(name)
        for flags, kwargs in specs:
            sp.add_argument(flags, **kwargs)
        sp.set_defaults(fn=fn)
        return sp

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
    add("reprice", cmd_reprice,
        ("--trip", {"required": True}),
        ("--traveler", {"default": None}))
    add("confirm", cmd_confirm,
        ("--trip", {"required": True}),
        ("--intent", {"required": True}),
        ("--user-confirmed", {"required": True,
                              "help": "must be literally 'yes'"}))
    add("book", cmd_book,
        ("--trip", {"required": True}),
        ("--intent", {"required": True}),
        ("--traveler", {"default": None}),
        ("--payment-kind", {"default": "balance"}),
        ("--payment-token", {"default": None}))
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
        ("--port", {"type": int, "default": 8090}))
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    app = App()
    args.fn(app, args)


if __name__ == "__main__":
    main()
