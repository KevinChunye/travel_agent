"""Persistent monitoring tasks for booked travel.

Tasks are stored in the repository and executed by an external scheduled
trigger (Maritime cron calling ``python -m src.cli monitor-run``), never
by an in-process timer, so they survive restarts and redeploys.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class MonitoringTaskKind(str, Enum):
    DEPARTURE_REMINDER = "departure_reminder"
    CHECK_IN_REMINDER = "check_in_reminder"
    STATUS_CHECK = "status_check"  # schedule changes, cancellations, delays


class MonitoringTask(BaseModel):
    id: str = Field(default_factory=lambda: f"mon_{uuid.uuid4().hex[:12]}")
    booking_id: str
    user_id: str
    kind: MonitoringTaskKind
    due_at: datetime
    # For recurring tasks (status checks): re-arm this many minutes after
    # each run until the trip departs. None means one-shot.
    recur_minutes: Optional[int] = None
    active: bool = True
    # Snapshot of last observed provider state, for change detection.
    last_snapshot: dict = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class TravelChange(BaseModel):
    """A relevant change detected on a booking — the only thing that
    triggers a user notification."""

    booking_id: str
    kind: str  # schedule_change | cancellation | delay | terminal_change
    summary: str
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
