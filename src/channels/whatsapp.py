"""WhatsApp Cloud API channel adapter.

Only this module knows about WhatsApp payload shapes and Graph API
endpoints. Configuration comes from environment variables:

- WHATSAPP_ACCESS_TOKEN
- WHATSAPP_PHONE_NUMBER_ID
- WHATSAPP_VERIFY_TOKEN (webhook handshake)

Note: when deployed behind OpenClaw's own WhatsApp channel, OpenClaw
handles transport and this adapter is unused — the skill still speaks
through the same MessagingChannel-shaped text produced by the services.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from src.channels.base import IncomingMessage, MessagingChannel

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.facebook.com/v20.0"


class WhatsAppChannel(MessagingChannel):
    name = "whatsapp"

    def __init__(
        self,
        access_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN")
        self._phone_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        self._timeout = timeout

    # -- inbound -------------------------------------------------------------

    def receive_message(self, raw_payload: Any) -> list[IncomingMessage]:
        """Parse a WhatsApp Cloud API webhook body into IncomingMessages.

        Non-text messages (media, reactions, statuses) are ignored for now.
        """
        messages: list[IncomingMessage] = []
        for entry in (raw_payload or {}).get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []) or []:
                    if msg.get("type") != "text":
                        continue
                    messages.append(
                        IncomingMessage(
                            channel=self.name,
                            user_id=msg.get("from", ""),
                            text=(msg.get("text") or {}).get("body", ""),
                            message_id=msg.get("id"),
                        )
                    )
        return messages

    @staticmethod
    def verify_webhook(params: dict[str, str]) -> Optional[str]:
        """Meta webhook handshake: return hub.challenge when the token
        matches, else None."""
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN")
        ):
            return params.get("hub.challenge")
        return None

    # -- outbound ------------------------------------------------------------

    def _post(self, payload: dict) -> None:
        if not self._token or not self._phone_id:
            logger.warning("WhatsApp not configured; dropping outbound message")
            return
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{_GRAPH_BASE}/{self._phone_id}/messages",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
            )
            if resp.status_code >= 400:
                logger.error(
                    "WhatsApp send failed (%s): %s",
                    resp.status_code,
                    resp.text[:300],
                )

    def send_message(self, user_id: str, text: str) -> None:
        self._post(
            {
                "messaging_product": "whatsapp",
                "to": user_id,
                "type": "text",
                "text": {"body": text[:4096]},
            }
        )

    def send_notification(self, user_id: str, text: str) -> None:
        # Plain text works inside the 24h customer-service window; outside
        # it, WhatsApp requires an approved template — swap here when one
        # is registered.
        self.send_message(user_id, text)
