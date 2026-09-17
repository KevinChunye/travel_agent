"""Post-booking monitoring.

Creates persistent tasks when a booking confirms and processes the ones
that are due. ``run_due`` is the single entrypoint, meant to be invoked
by an external scheduler (a Maritime scheduled trigger or cron running
``python -m src.cli monitor-run``) — never an in-process timer, so a
restart or redeploy loses nothing.

Users are only notified when something relevant changes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.channels.base import MessagingChannel
from src.models.booking import Booking, BookingStatus
from src.models.monitoring import MonitoringTask, MonitoringTaskKind, TravelChange
from src.models.travel_request import TransportMode
from src.providers.base import ProviderError, TravelProvider, TripStatus
from src.storage.repository import Repository

logger = logging.getLogger(__name__)

#: Delay below this many minutes is noise, not a notification.
MEANINGFUL_DELAY_MINUTES = 20


class MonitoringService:
    def __init__(
        self,
        providers: list[TravelProvider],
        repo: Repository,
        channel: Optional[MessagingChannel] = None,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._providers = {p.name: p for p in providers}
        self._repo = repo
        self._channel = channel
        self._clock = clock

    # -- task creation --------------------------------------------------------

    def create_tasks_for_booking(self, booking: Booking) -> list[MonitoringTask]:
        dep = booking.offer.departure
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        tasks = [
            MonitoringTask(
                booking_id=booking.id,
                user_id=booking.user_id,
                kind=MonitoringTaskKind.DEPARTURE_REMINDER,
                due_at=dep - timedelta(hours=24),
            ),
            MonitoringTask(
                booking_id=booking.id,
                user_id=booking.user_id,
                kind=MonitoringTaskKind.DEPARTURE_REMINDER,
                due_at=dep - timedelta(hours=3),
            ),
            MonitoringTask(
                booking_id=booking.id,
                user_id=booking.user_id,
                kind=MonitoringTaskKind.STATUS_CHECK,
                due_at=self._clock(),
                recur_minutes=180,
            ),
        ]
        if booking.offer.mode == TransportMode.FLIGHT:
            tasks.append(
                MonitoringTask(
                    booking_id=booking.id,
                    user_id=booking.user_id,
                    kind=MonitoringTaskKind.CHECK_IN_REMINDER,
                    due_at=dep - timedelta(hours=24),
                )
            )
        for t in tasks:
            self._repo.save_monitoring_task(t)
        return tasks

    # -- execution ------------------------------------------------------------

    def run_due(self, now: Optional[datetime] = None) -> list[TravelChange]:
        """Process every due task. Returns the relevant changes found."""
        now = now or self._clock()
        changes: list[TravelChange] = []
        for task in self._repo.due_monitoring_tasks(now):
            booking = self._repo.get_booking(task.booking_id)
            if booking is None or booking.status in (
                BookingStatus.CANCELLED,
            ):
                task.active = False
                self._repo.save_monitoring_task(task)
                continue
            try:
                if task.kind == MonitoringTaskKind.STATUS_CHECK:
                    change = self._run_status_check(task, booking)
                    if change:
                        changes.append(change)
                else:
                    self._send_reminder(task, booking)
            except ProviderError as exc:
                logger.warning("monitoring task %s failed: %s", task.id, exc)
            self._reschedule(task, booking, now)
        return changes

    def _reschedule(self, task: MonitoringTask, booking: Booking, now: datetime) -> None:
        dep = booking.offer.departure
        if dep.tzinfo is None:
            dep = dep.replace(tzinfo=timezone.utc)
        if task.recur_minutes and now < dep:
            task.due_at = now + timedelta(minutes=task.recur_minutes)
        else:
            task.active = False
        self._repo.save_monitoring_task(task)

    def _run_status_check(
        self, task: MonitoringTask, booking: Booking
    ) -> Optional[TravelChange]:
        provider = self._providers.get(booking.provider)
        if provider is None:
            return None
        status = provider.get_status(booking.provider_order_id)
        snapshot = status.snapshot()
        previous = task.last_snapshot
        task.last_snapshot = snapshot
        self._repo.save_monitoring_task(task)
        if not previous:
            return None  # first observation is the baseline, not a change
        change = self._diff(previous, snapshot, booking)
        if change is not None:
            if status.status == TripStatus.CANCELLED:
                booking.status = BookingStatus.CANCELLED
            elif change.kind in ("schedule_change", "delay"):
                booking.status = BookingStatus.CHANGED
            self._repo.save_booking(booking)
            self._notify(booking.user_id, change.summary)
        return change

    def _diff(self, old: dict, new: dict, booking: Booking) -> Optional[TravelChange]:
        ref = booking.booking_reference or booking.provider_order_id
        if new.get("status") == "cancelled" and old.get("status") != "cancelled":
            return TravelChange(
                booking_id=booking.id,
                kind="cancellation",
                summary=f"⚠️ Your booking {ref} was CANCELLED by the carrier.",
            )
        if old.get("departure") and new.get("departure") and (
            old["departure"] != new["departure"]
        ):
            old_dep = datetime.fromisoformat(old["departure"])
            new_dep = datetime.fromisoformat(new["departure"])
            delta_min = abs((new_dep - old_dep).total_seconds()) / 60
            if delta_min >= MEANINGFUL_DELAY_MINUTES:
                kind = "delay" if new_dep > old_dep else "schedule_change"
                return TravelChange(
                    booking_id=booking.id,
                    kind=kind,
                    summary=(
                        f"Schedule update for {ref}: departure moved from "
                        f"{old_dep:%a %H:%M} to {new_dep:%a %H:%M}."
                    ),
                )
        for key, label in (
            ("origin_terminal", "departure terminal"),
            ("destination_terminal", "arrival terminal"),
        ):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                return TravelChange(
                    booking_id=booking.id,
                    kind="terminal_change",
                    summary=f"{ref}: {label} changed from {old[key]} to {new[key]}.",
                )
        return None

    def _send_reminder(self, task: MonitoringTask, booking: Booking) -> None:
        o = booking.offer
        ref = booking.booking_reference or booking.provider_order_id
        if task.kind == MonitoringTaskKind.CHECK_IN_REMINDER:
            text = (
                f"Check-in is open for {ref}: "
                f"{o.carrier_name or o.carrier} {o.origin} → {o.destination}, "
                f"departing {o.departure:%a %b %d %H:%M}."
            )
        else:
            text = (
                f"Reminder: {ref} departs {o.departure:%a %b %d at %H:%M} "
                f"from {o.origin}."
            )
        self._notify(task.user_id, text)

    def _notify(self, user_id: str, text: str) -> None:
        if self._channel is not None:
            self._channel.send_notification(user_id, text)
        else:
            logger.info("notification for %s: %s", user_id, text)
