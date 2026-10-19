"""Proactive monitoring: cheap rules decide when to warn, the LLM only looks when needed."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..ha.index import OPENING_CLASSES, SAFETY_CLASSES, EntityInfo
from ..notify import ACTION_PREFIX_CONFIRM, ACTION_PREFIX_REJECT, Notifier
from ..tools.actions import describe_service, queue_confirmation, resolve_confirmation
from ..tools.base import ToolContext

_LOGGER = logging.getLogger(__name__)

SAFETY_LABELS = {
    "smoke": "rook",
    "gas": "gas",
    "carbon_monoxide": "koolmonoxide",
    "moisture": "lekkage",
    "safety": "een onveilige situatie",
    "heat": "hitte",
}
NOTHING_MARKER = "NIETS"
# After (re)connecting, Home Assistant may still be starting: entities, areas and states settle over
# a minute or two and sensors flip on. Ignore detections and periodic rules during that time.
STARTUP_GRACE_SECONDS = 120


def minutes_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        changed = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - changed).total_seconds() / 60


def is_heating(ent: EntityInfo) -> bool:
    if ent.domain != "climate" or ent.state in ("off", "unavailable", "unknown"):
        return False
    action = ent.attributes.get("hvac_action")
    if action is not None:
        return action in ("heating", "preheating")
    current, target = ent.attributes.get("current_temperature"), ent.attributes.get("temperature")
    if ent.state in ("heat", "heat_cool", "auto") and current is not None and target is not None:
        return float(current) < float(target)
    return False


def is_unsecured(ent: EntityInfo) -> bool:
    if ent.domain == "lock":
        return ent.state in ("unlocked", "open")
    if ent.domain == "cover":
        garage = ent.device_class in ("garage", "gate") or any(w in ent.entity_id for w in ("garage", "poort", "gate"))
        return garage and ent.state in ("open", "opening")
    return False


class Watcher:
    def __init__(self, ctx: ToolContext, notifier: Notifier) -> None:
        self._ctx = replace(ctx, source="watcher", conversation_id="watcher")
        self._notifier = notifier
        self._cooldowns: dict[str, float] = {}
        self._done: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._grace_until = 0.0

    @property
    def config(self):
        return self._ctx.config

    def set_timezone(self, tz: str | None) -> None:
        self._ctx = replace(self._ctx, timezone=tz)

    def start_grace(self, seconds: float = STARTUP_GRACE_SECONDS) -> None:
        self._grace_until = max(self._grace_until, time.monotonic() + seconds)

    async def on_connected(self) -> None:
        self.start_grace()

    def on_ha_started(self, _data: dict[str, Any]) -> None:
        self.start_grace()

    @property
    def in_grace(self) -> bool:
        return time.monotonic() < self._grace_until

    def _allowed(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self._cooldowns.get(key, -1e9) < seconds:
            return False
        self._cooldowns[key] = now
        return True

    def _once(self, key: str) -> bool:
        if key in self._done:
            return False
        self._done.add(key)
        if len(self._done) > 5000:
            self._done.clear()
        return True

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for background analyses (used in tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- Event driven rules ------------------------------------------------------

    async def handle_state_changed(self, data: dict[str, Any]) -> None:
        if not self.config.proactive:
            return
        new, old = data.get("new_state"), data.get("old_state")
        if not new or not old or new.get("state") == old.get("state"):
            return
        if old.get("state") in ("unavailable", "unknown"):
            return
        ent = self._ctx.index.get(new["entity_id"])
        if ent is None or ent.domain != "binary_sensor" or ent.state != "on":
            return
        if ent.device_class in SAFETY_CLASSES:
            await self._safety_alert(ent)
        elif self._ctx.index.is_detection_sensor(ent) and not self.in_grace:
            self._spawn(self._detection(ent))

    async def _safety_alert(self, ent: EntityInfo) -> None:
        if not self._allowed(f"safety:{ent.entity_id}", 600):
            return
        label = SAFETY_LABELS.get(ent.device_class or "", "een alarm")
        where = f" in {ent.area}" if ent.area else ""
        await self._notifier.send(
            f"⚠️ Alarm: {label}",
            f"{ent.name}{where} detecteert {label}!",
            kind="safety",
            entity_id=ent.entity_id,
            critical=True,
            announce=f"Let op! {ent.name}{where} detecteert {label}.",
        )

    async def _detection(self, ent: EntityInfo) -> None:
        mode = self.config.camera_alerts
        if mode == "off":
            return
        index = self._ctx.index
        doorbell = index.is_doorbell(ent)
        home = index.anyone_home()
        if mode == "away" and not doorbell and home is not False:
            return
        camera = index.camera_for(ent)
        # One notification per device: person, vehicle and motion sensors of one camera fire together.
        key = ent.device_id or (camera.entity_id if camera else ent.entity_id)
        if not self._allowed(f"{'ring' if doorbell else 'detect'}:{key}", 60 if doorbell else 300):
            return
        where = ent.area or (camera.name if camera else ent.name)

        if camera is None:
            await self._notifier.send(
                "🚶 Beweging terwijl niemand thuis is",
                f"{ent.name} ({where}) detecteert beweging.",
                kind="motion",
                entity_id=ent.entity_id,
            )
            return

        description, failure = "", ""
        try:
            image = await self._ctx.ha.camera_snapshot(camera.entity_id)
            description = await self._ctx.llm.describe_image(
                image,
                f"Dit is het actuele beeld van camera '{camera.name}'. De sensor '{ent.name}' ging net af. "
                "Beschrijf in maximaal twee zinnen wie of wat je ziet (personen, voertuigen, dieren, pakketjes) "
                f"en of het verdacht lijkt. Begin je antwoord met '{NOTHING_MARKER}' als er niets relevants "
                "te zien is. Antwoord in het Nederlands.",
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Camera analysis for %s failed: %s", camera.entity_id, err)
            failure = f" (geen beeldanalyse: {str(err)[:120] or type(err).__name__})"

        if description.upper().startswith(NOTHING_MARKER) and not doorbell:
            self._ctx.db.add_event("camera", f"Detectie bij {where}", description, camera.entity_id, False)
            return
        title = "🔔 Er wordt aangebeld" if doorbell else f"📷 Detectie bij {where}"
        message = description.removeprefix(NOTHING_MARKER).strip(" :.") or f"{ent.name} ging af.{failure}"
        await self._notifier.send(title, message, kind="camera", entity_id=camera.entity_id,
                                  image_entity=camera.entity_id,
                                  announce=f"Er wordt aangebeld. {message}" if doorbell else None,
                                  respect_quiet_hours=not doorbell)

    async def handle_notification_action(self, data: dict[str, Any]) -> None:
        """Buttons pressed in a Companion app notification."""
        action = data.get("action") or ""
        for prefix, approve in ((ACTION_PREFIX_CONFIRM, True), (ACTION_PREFIX_REJECT, False)):
            if action.startswith(prefix):
                result = await resolve_confirmation(self._ctx, action[len(prefix):], approve)
                status = result.get("status")
                text = {
                    "ok": "✅ Uitgevoerd",
                    "dry_run": "🧪 Testmodus: niet uitgevoerd",
                    "rejected": "Geannuleerd",
                }.get(status, f"❌ {result.get('error', status)}")
                await self._notifier.send(text, result.get("description", ""), kind="confirmation_result",
                                          respect_quiet_hours=False)
                return

    # --- Periodic rules --------------------------------------------------------------

    async def tick(self) -> None:
        if not self.config.proactive or self.in_grace:
            return
        await self._window_and_heating()
        await self._unsecured_while_away()

    async def _window_and_heating(self) -> None:
        entities = list(self._ctx.index.entities.values())
        heating = [e for e in entities if is_heating(e)]
        if not heating:
            return
        for ent in entities:
            if ent.domain != "binary_sensor" or ent.device_class not in OPENING_CLASSES or ent.state != "on":
                continue
            if ent.device_class == "garage_door":
                continue
            open_minutes = minutes_since(ent.last_changed)
            if open_minutes < self.config.open_window_minutes:
                continue
            nearby = [h for h in heating if not ent.area or not h.area or h.area == ent.area]
            if not nearby or not self._once(f"window:{ent.entity_id}:{ent.last_changed}"):
                continue
            await self._notifier.send(
                "🪟 Raam open, verwarming aan",
                f"{ent.name} staat al {int(open_minutes)} minuten open terwijl {nearby[0].name} verwarmt.",
                kind="energy",
                entity_id=ent.entity_id,
            )

    async def _unsecured_while_away(self) -> None:
        index = self._ctx.index
        if index.anyone_home() is not False:
            return
        people = [e for e in index.entities.values() if e.domain == "person"]
        away_minutes = min(minutes_since(p.last_changed) for p in people)
        if away_minutes < self.config.unlocked_away_minutes:
            return
        left_at = max((p.last_changed or "") for p in people)
        for ent in list(index.entities.values()):
            if not is_unsecured(ent) or not self._once(f"away:{ent.entity_id}:{left_at}"):
                continue
            domain, service = ("lock", "lock") if ent.domain == "lock" else ("cover", "close_cover")
            args = {"domain": domain, "service": service, "entity_ids": [ent.entity_id], "data": {}}
            await queue_confirmation(
                self._ctx,
                "service",
                args,
                f"{ent.name} staat open terwijl niemand thuis is. "
                f"{'Op slot doen' if domain == 'lock' else 'Sluiten'}? ({describe_service(domain, service, [ent.entity_id], {})})",
                f"niemand is al {int(away_minutes)} minuten thuis",
            )
