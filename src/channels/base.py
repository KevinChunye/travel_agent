"""Messaging channel abstraction.

Travel search/booking modules never know which channel they are talking
to. WhatsApp is the first implementation; iMessage (e.g. via a Photon
adapter) can be added later by implementing this same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class IncomingMessage(BaseModel):
    """Channel-agnostic inbound message."""

    channel: str
    user_id: str  # channel-scoped stable user id (e.g. WhatsApp wa_id)
    text: str
    message_id: Optional[str] = None
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MessagingChannel(ABC):
    name: str = "abstract"

    @abstractmethod
    def receive_message(self, raw_payload: Any) -> list[IncomingMessage]:
        """Parse a raw inbound payload (e.g. a webhook body) into messages."""

    @abstractmethod
    def send_message(self, user_id: str, text: str) -> None:
        """Send a conversational reply."""

    @abstractmethod
    def send_notification(self, user_id: str, text: str) -> None:
        """Send a proactive notification (may use a different mechanism,
        e.g. WhatsApp template messages outside the 24h session window)."""
