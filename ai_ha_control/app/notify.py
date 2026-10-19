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
ACTION_PREFIX_UNDO = "AIHA_UNDO_"
ACTION_PREFIX_FEEDBACK_UP = "AIHA_FB_UP_"
ACTION_PREFIX_FEEDBACK_DOWN = "AIHA_FB_DOWN_"
MAX_ACTIONS = 3  # Android shows at most three buttons

# Not urgent: bundled in the evening digest when one is configured.
DIGEST_KINDS = {"anomaly", "energy", "device", "comfort", "routine"}
# Only for the people at home (who can do something about it there).
HOME_KINDS = {"doorbell", "energy", "comfort"}

# Kinds where 👍/👎 feedback makes sense (things the AI decided to tell you).
FEEDBACK_KINDS = {"camera", "motion", "energy", "anomaly", "agent", "autonomous", "presence", "security", "summary",
                  "comfort"}


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
        self.index = None  # EntityIndex, wired up by the app: who is home, for routing per person

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
        ref: str | None = None,
        incident_id: int | None = None,
    ) -> int:
        """Log the event and push it unless quiet hours apply. Returns the event id."""
        event_id = self._db.add_event(kind, title, message, entity_id, False, ref, incident_id)
        quiet = respect_quiet_hours and not critical and in_quiet_hours(self._config.quiet_hours, self.now())
        if quiet:
            return event_id
        if self._config.digest_time and kind in DIGEST_KINDS and not critical and not actions:
            return event_id  # goes into the evening digest
        buttons = list(actions or [])
        if kind in FEEDBACK_KINDS:
            buttons += [{"action": f"{ACTION_PREFIX_FEEDBACK_UP}{event_id}", "title": "👍 Nuttig"},
                        {"action": f"{ACTION_PREFIX_FEEDBACK_DOWN}{event_id}", "title": "👎 Onnodig"}]
        tag = f"aiha_{ref or kind}_{incident_id or entity_id or ''}"
        try:
            await self._push(title, message, tag, image_entity, buttons[:MAX_ACTIONS], critical, kind)
            self._db.set_event_pushed(event_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Notification failed: %s", err)
        if announce:
            await self.announce(announce)
        return event_id

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
        kind: str = "info",
    ) -> None:
        services = self.targets(kind, critical)
        if not services:
            # Same tag = same notification: updates replace each other instead of piling up.
            await self._ha.call_service(
                "persistent_notification", "create", {"title": title, "message": message, "notification_id": tag}
            )
            return
        data: dict[str, Any] = {"tag": tag}
        if image_entity:
            data["image"] = f"/api/camera_proxy/{image_entity}"
            data["entity_id"] = image_entity
        if actions:
            data["actions"] = actions
        if critical:
            data.update({"priority": "high", "ttl": 0, "channel": "alarm_stream",
                         "push": {"interruption-level": "critical"}})
        for service in services:
            domain, _, name = service.partition(".")
            if not name:
                domain, name = "notify", domain
            await self._ha.call_service(domain, name, {"title": title, "message": message, "data": data})

    def people(self) -> dict[str, str]:
        """person entity -> notify service, from the person_notify option."""
        result = {}
        for item in self._config.person_notify:
            person, _, service = item.partition("=")
            if person.strip() and service.strip():
                result[person.strip()] = service.strip()
        return result

    def targets(self, kind: str, critical: bool = False) -> list[str]:
        """Where a notification goes: everybody, or for doorbell-like things only the people at home."""
        people = self.people()
        if not people:
            service = self._config.notify_service.strip()
            return [service] if service else []
        if kind in HOME_KINDS and not critical and self.index is not None:
            home = [svc for person, svc in people.items()
                    if (ent := self.index.get(person)) is not None and ent.state == "home"]
            if home:
                return home
        return list(dict.fromkeys(people.values()))

    async def send_digest(self, since: float) -> str:
        """One notification with everything that waited for the digest."""
        events = [e for e in self._db.events(since=since, limit=50)
                  if e["kind"] in DIGEST_KINDS and not e["pushed"]]
        if not events:
            return ""
        lines = [f"• {e['title']}: {e['message']}" for e in reversed(events[:15])]
        text = "\n".join(lines) + (f"\n… en nog {len(events) - 15}" if len(events) > 15 else "")
        title = "📋 Overzicht van vandaag"
        await self._push(title, text, "aiha_digest", None, None, False, "digest")
        for event in events:
            self._db.set_event_pushed(event["id"])
        return text

    async def ask_confirmation(self, pending: PendingAction) -> None:
        await self.send(
            "Bevestiging nodig" if self._config.language == "nl" else "Confirmation needed",
            f"{pending.description}\n({pending.reason})",
            kind="confirmation",
            ref=f"confirm:{pending.id}",
            actions=[
                {"action": f"{ACTION_PREFIX_CONFIRM}{pending.id}",
                 "title": "Uitvoeren" if self._config.language == "nl" else "Execute"},
                {"action": f"{ACTION_PREFIX_REJECT}{pending.id}",
                 "title": "Annuleren" if self._config.language == "nl" else "Cancel"},
            ],
            respect_quiet_hours=False,
        )

    async def action_done(self, action_id: int, description: str, reason: str = "") -> None:
        """Tell the user what the agent did by itself, with an undo button."""
        message = description + (f"\nWaarom: {reason}" if reason else "")
        await self.send(
            "🤖 Zelf gedaan",
            message,
            kind="autonomous",
            ref=f"action:{action_id}",
            actions=[{"action": f"{ACTION_PREFIX_UNDO}{action_id}", "title": "↩️ Ongedaan maken"}],
        )
