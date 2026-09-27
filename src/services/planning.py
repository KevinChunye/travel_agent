"""Durable planning checkpoints; research is unverified until human/agent review."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

class ResearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["hotel", "restaurant", "transport", "activity"]
    name: str = Field(min_length=1, max_length=160)
    source_url: HttpUrl
    checked_at: datetime
    rationale: str = Field(min_length=1, max_length=1200)
    caveats: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def timestamp(self):
        if self.checked_at.tzinfo is None or self.checked_at > datetime.now(timezone.utc):
            raise ValueError("checked_at must have a timezone and cannot be in the future")
        return self

class PlanCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["planning", "researching", "review", "ready", "blocked"]
    next_action: str = Field(min_length=1, max_length=1200)
    research: list[ResearchItem] = Field(default_factory=list, max_length=12)
    reviewed: bool = False
    unresolved: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ready(self):
        if self.phase == "ready" and (not self.reviewed or self.unresolved or not self.research):
            raise ValueError("ready requires cited research, parent review and no unresolved blockers")
        return self

class PlanningStore:
    def __init__(self, repo):
        self.repo = repo
        repo._execute("CREATE TABLE IF NOT EXISTS planning_checkpoints (trip_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        repo._execute("CREATE TABLE IF NOT EXISTS planning_events (id INTEGER PRIMARY KEY, trip_id TEXT NOT NULL, phase TEXT NOT NULL, created_at TEXT NOT NULL)")

    def save(self, trip_id, checkpoint):
        if not self.repo.get_trip(trip_id):
            raise ValueError("Unknown trip")
        # A new research attempt or failure must not erase previously cited findings. Reset them
        # explicitly in a planning checkpoint when starting a different task.
        previous = self.get(trip_id)
        if checkpoint.phase != "planning" and not checkpoint.research and previous:
            checkpoint = checkpoint.model_copy(update={"research": previous.research})
        # Checkpoint and event commit together, so restart cannot lose the audit event.
        with self.repo._lock, self.repo._conn:
            self.repo._conn.execute("INSERT INTO planning_checkpoints VALUES (?,?) ON CONFLICT(trip_id) DO UPDATE SET data=excluded.data", (trip_id, checkpoint.model_dump_json()))
            self.repo._conn.execute("INSERT INTO planning_events(trip_id,phase,created_at) VALUES (?,?,?)", (trip_id, checkpoint.phase, datetime.now(timezone.utc).isoformat()))

    def get(self, trip_id):
        row = self.repo._execute("SELECT data FROM planning_checkpoints WHERE trip_id=?", (trip_id,)).fetchone()
        return PlanCheckpoint.model_validate_json(row[0]) if row else None

    def events(self, trip_id):
        return [dict(zip(("id", "phase", "created_at"), row)) for row in self.repo._execute("SELECT id,phase,created_at FROM planning_events WHERE trip_id=? ORDER BY id", (trip_id,)).fetchall()]
