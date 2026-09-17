"""Persistence layer.

An abstract Repository defines everything the services need; the SQLite
implementation is the initial backend. A PostgreSQL implementation can
be swapped in later without touching services.

Design notes:
- Booking state lives here, never only in conversation history.
- Traveler PII sits in its own table (``travelers``), separate from
  trips/conversation data, so it can be encrypted or migrated on its own.
- No payment card data is ever stored anywhere.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models.booking import Booking, BookingIntent, PassengerIdentity, Trip, TripState
from src.models.monitoring import MonitoringTask
from src.models.offer import Offer
from src.models.preferences import SelectionFeedback, UserPreferences


class Repository(ABC):
    # -- trips
    @abstractmethod
    def save_trip(self, trip: Trip) -> None: ...
    @abstractmethod
    def get_trip(self, trip_id: str) -> Optional[Trip]: ...
    @abstractmethod
    def latest_trip_for_user(self, user_id: str) -> Optional[Trip]: ...

    # -- offers
    @abstractmethod
    def save_offers(self, trip_id: str, offers: list[Offer]) -> None: ...
    @abstractmethod
    def get_offer(self, offer_id: str) -> Optional[Offer]: ...
    @abstractmethod
    def get_offers_for_trip(self, trip_id: str) -> list[Offer]: ...

    # -- booking intents
    @abstractmethod
    def save_intent(self, intent: BookingIntent) -> None: ...
    @abstractmethod
    def get_intent(self, intent_id: str) -> Optional[BookingIntent]: ...

    # -- bookings
    @abstractmethod
    def save_booking(self, booking: Booking) -> None: ...
    @abstractmethod
    def get_booking(self, booking_id: str) -> Optional[Booking]: ...
    @abstractmethod
    def list_bookings_for_user(self, user_id: str) -> list[Booking]: ...

    # -- preferences and PII
    @abstractmethod
    def save_preferences(self, prefs: UserPreferences) -> None: ...
    @abstractmethod
    def get_preferences(self, user_id: str) -> Optional[UserPreferences]: ...
    @abstractmethod
    def save_traveler(self, traveler: PassengerIdentity) -> None: ...
    @abstractmethod
    def get_travelers(self, user_id: str) -> list[PassengerIdentity]: ...

    # -- monitoring
    @abstractmethod
    def save_monitoring_task(self, task: MonitoringTask) -> None: ...
    @abstractmethod
    def due_monitoring_tasks(self, now: datetime) -> list[MonitoringTask]: ...
    @abstractmethod
    def tasks_for_booking(self, booking_id: str) -> list[MonitoringTask]: ...

    # -- feedback
    @abstractmethod
    def record_feedback(self, feedback: SelectionFeedback) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trips_user ON trips(user_id, updated_at);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offers_trip ON offers(trip_id);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id);

CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

-- Traveler PII: intentionally its own table, separate from chat state.
CREATE TABLE IF NOT EXISTS travelers (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_travelers_user ON travelers(user_id);

CREATE TABLE IF NOT EXISTS monitoring_tasks (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    active INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON monitoring_tasks(active, due_at);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class SQLiteRepository(Repository):
    def __init__(self, path: str | Path = "data/travel_agent.sqlite3") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # -- trips
    def save_trip(self, trip: Trip) -> None:
        trip.updated_at = datetime.now(timezone.utc)
        self._execute(
            "INSERT INTO trips(id, user_id, state, updated_at, data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "updated_at=excluded.updated_at, data=excluded.data",
            (
                trip.id,
                trip.user_id,
                trip.state.value,
                _utc_iso(trip.updated_at),
                trip.model_dump_json(),
            ),
        )

    def get_trip(self, trip_id: str) -> Optional[Trip]:
        row = self._execute("SELECT data FROM trips WHERE id=?", (trip_id,)).fetchone()
        return Trip.model_validate_json(row[0]) if row else None

    def latest_trip_for_user(self, user_id: str) -> Optional[Trip]:
        row = self._execute(
            "SELECT data FROM trips WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return Trip.model_validate_json(row[0]) if row else None

    # -- offers
    def save_offers(self, trip_id: str, offers: list[Offer]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO offers(id, trip_id, data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                [(o.id, trip_id, o.model_dump_json()) for o in offers],
            )
            self._conn.commit()

    def get_offer(self, offer_id: str) -> Optional[Offer]:
        row = self._execute("SELECT data FROM offers WHERE id=?", (offer_id,)).fetchone()
        return Offer.model_validate_json(row[0]) if row else None

    def get_offers_for_trip(self, trip_id: str) -> list[Offer]:
        rows = self._execute(
            "SELECT data FROM offers WHERE trip_id=?", (trip_id,)
        ).fetchall()
        return [Offer.model_validate_json(r[0]) for r in rows]

    # -- intents
    def save_intent(self, intent: BookingIntent) -> None:
        self._execute(
            "INSERT INTO intents(id, trip_id, data) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (intent.id, intent.trip_id, intent.model_dump_json()),
        )

    def get_intent(self, intent_id: str) -> Optional[BookingIntent]:
        row = self._execute(
            "SELECT data FROM intents WHERE id=?", (intent_id,)
        ).fetchone()
        return BookingIntent.model_validate_json(row[0]) if row else None

    # -- bookings
    def save_booking(self, booking: Booking) -> None:
        self._execute(
            "INSERT INTO bookings(id, trip_id, user_id, status, data) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
            (
                booking.id,
                booking.trip_id,
                booking.user_id,
                booking.status.value,
                booking.model_dump_json(),
            ),
        )

    def get_booking(self, booking_id: str) -> Optional[Booking]:
        row = self._execute(
            "SELECT data FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()
        return Booking.model_validate_json(row[0]) if row else None

    def list_bookings_for_user(self, user_id: str) -> list[Booking]:
        rows = self._execute(
            "SELECT data FROM bookings WHERE user_id=?", (user_id,)
        ).fetchall()
        return [Booking.model_validate_json(r[0]) for r in rows]

    # -- preferences / PII
    def save_preferences(self, prefs: UserPreferences) -> None:
        self._execute(
            "INSERT INTO preferences(user_id, data) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (prefs.user_id, prefs.model_dump_json()),
        )

    def get_preferences(self, user_id: str) -> Optional[UserPreferences]:
        row = self._execute(
            "SELECT data FROM preferences WHERE user_id=?", (user_id,)
        ).fetchone()
        return UserPreferences.model_validate_json(row[0]) if row else None

    def save_traveler(self, traveler: PassengerIdentity) -> None:
        self._execute(
            "INSERT INTO travelers(id, user_id, data) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (traveler.id, traveler.user_id, traveler.model_dump_json()),
        )

    def get_travelers(self, user_id: str) -> list[PassengerIdentity]:
        rows = self._execute(
            "SELECT data FROM travelers WHERE user_id=?", (user_id,)
        ).fetchall()
        return [PassengerIdentity.model_validate_json(r[0]) for r in rows]

    # -- monitoring
    def save_monitoring_task(self, task: MonitoringTask) -> None:
        self._execute(
            "INSERT INTO monitoring_tasks(id, booking_id, user_id, due_at, active, data) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET due_at=excluded.due_at, "
            "active=excluded.active, data=excluded.data",
            (
                task.id,
                task.booking_id,
                task.user_id,
                _utc_iso(task.due_at),
                1 if task.active else 0,
                task.model_dump_json(),
            ),
        )

    def due_monitoring_tasks(self, now: datetime) -> list[MonitoringTask]:
        rows = self._execute(
            "SELECT data FROM monitoring_tasks WHERE active=1 AND due_at<=? "
            "ORDER BY due_at",
            (_utc_iso(now),),
        ).fetchall()
        return [MonitoringTask.model_validate_json(r[0]) for r in rows]

    def tasks_for_booking(self, booking_id: str) -> list[MonitoringTask]:
        rows = self._execute(
            "SELECT data FROM monitoring_tasks WHERE booking_id=?", (booking_id,)
        ).fetchall()
        return [MonitoringTask.model_validate_json(r[0]) for r in rows]

    # -- feedback
    def record_feedback(self, feedback: SelectionFeedback) -> None:
        self._execute(
            "INSERT INTO feedback(user_id, trip_id, data) VALUES(?,?,?)",
            (feedback.user_id, feedback.trip_id, feedback.model_dump_json()),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
