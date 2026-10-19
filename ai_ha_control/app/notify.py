"""Sending notifications to the user (Companion app or HA persistent notifications)."""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .config import Config
    from .ha.client import HAClient
    from .storage.db import Database
    from .tools.actions import PendingAction

_LOGGER = logging.getLogger(__name__)

ACTION_PREFIX_CONFIRM = "AIHA_CONFIRM_"
ACTION_PREFIX_REJECT = "AIHA_REJECT_"


def parse_quiet_hours(value: str | None) -> tuple[dtime, dtime] | None:
    if not value or "-" not in value:
        return None
    try:
        start, end = (dtime.fromisoformat(part.strip()) for part in value.split("-", 1))
    except ValueError:
        return None
    return start, end


def in_quiet_hours(value: str | None, now: datetime) -> bool:
    period = parse_quiet_hours(value)
    if period is None:
        return False
    start, end = period
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class Notifier:
    def __init__(self, config: Config, ha: HAClient, db: Database) -> None:
        self._config = config
        self._ha = ha
        self._db = db
        self.timezone: str | None = None

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone) if self.timezone else None)

    async def send(
        self,
        title: str,
        message: str,
        kind: str = "info",
        entity_id: str | None = None,
        image_entity: str | None = None,
        actions: list[dict[str, str]] | None = None,
        critical: bool = False,
        respect_quiet_hours: bool = True,
        announce: str | None = None,
    ) -> bool:
        """Log the event and push it unless quiet hours apply. Returns whether it was pushed."""
        quiet = respect_quiet_hours and not critical and in_quiet_hours(self._config.quiet_hours, self.now())
        pushed = False
        if not quiet:
            tag = f"aiha_{kind}_{entity_id or ''}"
            try:
                await self._push(title, message, tag, image_entity, actions, critical)
                pushed = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Notification failed: %s", err)
            if announce:
                await self.announce(announce)
        self._db.add_event(kind, title, message, entity_id, pushed)
        return pushed

    async def announce(self, message: str) -> None:
        """Speak a message on the configured speakers (TTS)."""
        players = [p for p in self._config.announce_players if p]
        if not players:
            return
        if not self._config.tts_entity:
            _LOGGER.warning("announce_players is set but tts_entity is empty")
            return
        try:
            await self._ha.call_service(
                "tts",
                "speak",
                {"media_player_entity_id": players, "message": message, "cache": False},
                {"entity_id": [self._config.tts_entity]},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Announcement failed: %s", err)

    async def _push(
        self,
        title: str,
        message: str,
        tag: str,
        image_entity: str | None,
        actions: list[dict[str, str]] | None,
        critical: bool,
    ) -> None:
        service = self._config.notify_service.strip()
        if not service:
            await self._ha.call_service(
                "persistent_notification", "create", {"title": title, "message": message}
            )
            return
        domain, _, name = service.partition(".")
        if not name:
            domain, name = "notify", domain
        data: dict[str, Any] = {"tag": tag}
        if image_entity:
            data["image"] = f"/api/camera_proxy/{image_entity}"
            data["entity_id"] = image_entity
        if actions:
            data["actions"] = actions
        if critical:
            data.update({"priority": "high", "ttl": 0, "channel": "alarm_stream",
                         "push": {"interruption-level": "critical"}})
        await self._ha.call_service(domain, name, {"title": title, "message": message, "data": data})

    async def ask_confirmation(self, pending: PendingAction) -> None:
        await self.send(
            "Bevestiging nodig" if self._config.language == "nl" else "Confirmation needed",
            f"{pending.description}\n({pending.reason})",
            kind="confirmation",
            entity_id=pending.id,
            actions=[
                {"action": f"{ACTION_PREFIX_CONFIRM}{pending.id}",
                 "title": "Uitvoeren" if self._config.language == "nl" else "Execute"},
                {"action": f"{ACTION_PREFIX_REJECT}{pending.id}",
                 "title": "Annuleren" if self._config.language == "nl" else "Cancel"},
            ],
            respect_quiet_hours=False,
        )
