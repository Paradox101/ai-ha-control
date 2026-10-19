"""Proactive monitoring: cheap rules decide when to warn, the LLM only looks when needed."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ..ha.context import DEVICE, RESIDENT
from ..ha.index import OPENING_CLASSES, SAFETY_CLASSES, EntityInfo
from ..autonomy import undo_action
from ..notify import (
    ACTION_PREFIX_CONFIRM,
    ACTION_PREFIX_FEEDBACK_DOWN,
    ACTION_PREFIX_FEEDBACK_UP,
    ACTION_PREFIX_REJECT,
    ACTION_PREFIX_UNDO,
    Notifier,
)
from ..tools.actions import describe_service, execute, queue_confirmation, resolve_confirmation
from ..tools.base import ToolContext
from .baseline import MODE_AWAY, MODE_SLEEP, MODE_VACATION, house_mode
from .vision import QUIET_CATEGORIES, SUSPICIOUS, UNKNOWN, classify

_LOGGER = logging.getLogger(__name__)

SAFETY_LABELS = {
    "smoke": "rook",
    "gas": "gas",
    "carbon_monoxide": "koolmonoxide",
    "moisture": "lekkage",
    "safety": "een onveilige situatie",
    "heat": "hitte",
}
# After (re)connecting, Home Assistant may still be starting: entities, areas and states settle over
# a minute or two and sensors flip on. Ignore detections and periodic rules during that time.
STARTUP_GRACE_SECONDS = 120
# Devices a resident controls; a manual change keeps the agent's hands off them for a while.
COMFORT_DOMAINS = {"light", "climate", "cover", "fan", "media_player", "switch", "humidifier", "water_heater"}
REVERSAL_SECONDS = 1800   # a resident changing an entity within this time after the agent did = "undo"
ARRIVAL_GRACE = 600       # a door opening this soon after someone came home is not an intrusion
ARMED = ("armed_away", "armed_home", "armed_night", "armed_vacation", "armed_custom_bypass")


def quote(text: str, limit: int = 200) -> str:
    """Camera descriptions are data for the agent, never instructions: mark and shorten them."""
    text = " ".join((text or "").split())
    return f"«{text[:limit]}{'…' if len(text) > limit else ''}»"


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
        self._last_arrival = 0.0
        # Optional collaborators, wired up by the app (tests may leave them out).
        self.agent = None       # AutonomousAgent: receives observations
        self.incidents = None   # IncidentManager: groups notifications into incidents
        self.baseline = None    # Baseline: learns activity patterns
        self.snapshots = None   # SnapshotStore: keeps images for incident timelines

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

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(self._ctx.timezone) if self._ctx.timezone else None)

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
        new, old = data.get("new_state"), data.get("old_state")
        if not new or not old or old.get("state") in ("unavailable", "unknown"):
            return
        setpoint = (new.get("attributes") or {}).get("temperature")
        setpoint_changed = (new["entity_id"].startswith("climate.")
                            and setpoint != (old.get("attributes") or {}).get("temperature"))
        if new.get("state") == old.get("state") and not setpoint_changed:
            return
        ent = self._ctx.index.get(new["entity_id"])
        if ent is None:
            return
        contexts = self._ctx.contexts
        origin, label = contexts.classify(new.get("context")) if contexts else (DEVICE, "")
        if not self.in_grace:
            await self._note_manual(ent, origin, old, new, setpoint_changed)
        if ent.domain == "person":
            self._note_arrival(ent, old.get("state"), new.get("state"))
        if not self.config.proactive or new.get("state") == old.get("state"):
            return
        if self.agent is not None and not self.in_grace:
            self.agent.note_state_change(ent, old.get("state"), new.get("state"), origin, label)
        if ent.domain == "alarm_control_panel" and new.get("state") == "triggered":
            await self._alarm_triggered(ent)
            return
        if ent.domain != "binary_sensor" or ent.state != "on":
            return
        if ent.device_class in OPENING_CLASSES and not self.in_grace:
            await self._opening_while_away(ent)
        if self.baseline is not None and not self.in_grace:
            anomaly = self.baseline.activity(ent, self._now())
            if anomaly:
                await self.report_anomaly(anomaly)
        if ent.device_class in SAFETY_CLASSES:
            await self._safety_alert(ent)
        elif self._ctx.index.is_detection_sensor(ent) and not self.in_grace:
            self._spawn(self._detection(ent))

    # --- Who did it: manual overrides and arrivals --------------------------------------

    async def _note_manual(self, ent: EntityInfo, origin: str, old: dict, new: dict, setpoint_changed: bool) -> None:
        """A resident (or the device itself) changed something: the agent backs off from it for a while."""
        if origin not in (RESIDENT, DEVICE) or ent.domain not in COMFORT_DOMAINS:
            return
        db, now = self._ctx.db, time.time()
        mode = house_mode(self._ctx.index, self.config, self._now())
        if ent.domain == "climate" and setpoint_changed and origin == RESIDENT:
            setpoint = (new.get("attributes") or {}).get("temperature")
            db.add_manual_change(ent.entity_id, mode, float(setpoint) if setpoint is not None else None)
        db.set_override(ent.entity_id, now + self.config.override_minutes * 60, f"aangepast door {origin}")
        action = db.last_action_on(ent.entity_id, now - REVERSAL_SECONDS)
        if action and not action["shadow"]:
            db.set_action_feedback(action["id"], -1)
            if self.agent is not None:
                self.agent.observe(f"De bewoner heeft je actie {action['domain']}.{action['service']} op {ent.name} "
                                   f"zelf teruggedraaid of aangepast; laat {ent.name} voorlopig met rust.")

    def _note_arrival(self, ent: EntityInfo, old: str | None, new: str | None) -> None:
        """First person home after everyone was away: remember the time (for preheating)."""
        if new != "home" or old == "home":
            return
        self._last_arrival = time.monotonic()
        others = [p for p in self._ctx.index.entities.values()
                  if p.domain == "person" and p.entity_id != ent.entity_id]
        if any(p.state == "home" for p in others):
            return
        now = self._now()
        self._ctx.db.add_arrival(now.weekday(), now.hour * 60 + now.minute)

    # --- Security rules (no LLM in the loop) ---------------------------------------------

    def _alarm_armed(self) -> bool:
        return any(e.domain == "alarm_control_panel" and e.state in ARMED for e in self._ctx.index.entities.values())

    async def _opening_while_away(self, ent: EntityInfo) -> None:
        """A door or window opens while nobody should be there: report at once, don't wait for the heartbeat."""
        mode = house_mode(self._ctx.index, self.config, self._now())
        armed = self._alarm_armed()
        if not armed and mode not in (MODE_AWAY, MODE_VACATION, MODE_SLEEP):
            return
        if time.monotonic() - self._last_arrival < ARRIVAL_GRACE and not armed:
            return  # someone just came home
        if mode == MODE_SLEEP and not armed and ent.device_class == "window":
            return  # windows open at night for fresh air
        if not self._allowed(f"intrusion:{ent.entity_id}", 300):
            return
        night = mode == MODE_SLEEP and not armed
        ref = f"anomaly:{ent.entity_id}:sleep-open" if night else None  # 👎 = "this is normal at night"
        if ref and self._ctx.db.is_muted(ref):
            return
        where = f" ({ent.area})" if ent.area else ""
        why = "het alarm staat aan" if armed else ("iedereen slaapt" if night else "niemand is thuis")
        text = f"{ent.name}{where} ging open terwijl {why}."
        if self.agent is not None:
            self.agent.observe(f"BEVEILIGING: {text}", urgent=True)
        camera = self._ctx.index.camera_for(ent)
        snapshot = None
        if camera is not None and self.snapshots is not None:
            try:
                snapshot = self.snapshots.save(camera.entity_id, await self._ctx.ha.camera_snapshot(camera.entity_id))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Snapshot for %s failed: %s", ent.entity_id, err)
        severity = "warning" if night else "critical"
        if self.incidents is not None:
            await self.incidents.report(
                "security", "Mogelijke inbraak" if not night else "Deur open in de nacht", text, severity=severity,
                entity_id=ent.entity_id, snapshot=snapshot, image_entity=camera.entity_id if camera else None,
                critical=not night, respect_quiet_hours=False, correlate_seconds=900, ref=ref)
            return
        await self._notifier.send("🚨 Mogelijke inbraak", text, kind="security", entity_id=ent.entity_id,
                                  image_entity=camera.entity_id if camera else None, critical=not night,
                                  respect_quiet_hours=False, ref=ref)

    async def _alarm_triggered(self, ent: EntityInfo) -> None:
        if not self._allowed(f"alarm:{ent.entity_id}", 600):
            return
        text = f"{ent.name}: het alarm is afgegaan!"
        if self.agent is not None:
            self.agent.observe(f"ALARM: {text}", urgent=True)
        if self.incidents is not None:
            await self.incidents.report("security", "Alarm afgegaan", text, severity="critical",
                                        entity_id=ent.entity_id, critical=True, announce=text)
            return
        await self._notifier.send("🚨 Alarm afgegaan", text, kind="security", entity_id=ent.entity_id, critical=True,
                                  announce=text)

    async def report_anomaly(self, anomaly) -> None:
        """Something outside the learned normal: one incident (👎 = 'this is normal') and tell the agent."""
        if self.agent is not None:
            self.agent.observe(f"Afwijking: {anomaly.text}", urgent=anomaly.severity != "info")
        if self.incidents is not None:
            await self.incidents.report(anomaly.key, "Ongebruikelijk", anomaly.text, severity=anomaly.severity,
                                        entity_id=anomaly.entity_id, ref=anomaly.key, correlate_seconds=6 * 3600)

    async def _safety_alert(self, ent: EntityInfo) -> None:
        if not self._allowed(f"safety:{ent.entity_id}", 600):
            return
        label = SAFETY_LABELS.get(ent.device_class or "", "een alarm")
        where = f" in {ent.area}" if ent.area else ""
        if self.agent is not None:
            self.agent.observe(f"ALARM: {ent.name}{where} detecteert {label}", urgent=True)
        if self.incidents is not None:
            await self.incidents.report(
                f"safety:{ent.entity_id}", f"Alarm: {label}", f"{ent.name}{where} detecteert {label}!",
                severity="critical", entity_id=ent.entity_id, critical=True,
                announce=f"Let op! {ent.name}{where} detecteert {label}.",
            )
        else:
            await self._send_safety(ent, label, where)
        await self._playbook(ent)

    async def _send_safety(self, ent: EntityInfo, label: str, where: str) -> None:
        await self._notifier.send(
            f"⚠️ Alarm: {label}",
            f"{ent.name}{where} detecteert {label}!",
            kind="safety",
            entity_id=ent.entity_id,
            critical=True,
            announce=f"Let op! {ent.name}{where} detecteert {label}.",
        )

    async def _playbook(self, ent: EntityInfo) -> list[str]:
        """Steps the user approved in advance for emergencies. Never doors or the alarm."""
        from ..security import is_security_entity

        config, index = self.config, self._ctx.index
        if self._ctx.autonomy is not None and not self._ctx.autonomy.enabled:
            return []
        steps: list[tuple[str, str, list[str], dict]] = []
        if ent.device_class == "moisture" and config.leak_valve:
            domain = config.leak_valve.split(".", 1)[0]
            steps.append(("valve", "close_valve", [config.leak_valve], {}) if domain == "valve"
                         else (domain, "turn_off", [config.leak_valve], {}))
        if ent.device_class in ("smoke", "gas", "carbon_monoxide", "heat"):
            if config.emergency_lights:
                lights = [e.entity_id for e in index.entities.values() if e.domain == "light" and e.state == "off"]
                if lights:
                    steps.append(("light", "turn_on", lights, {}))
            for fan in config.emergency_fans_off:
                steps.append((fan.split(".", 1)[0], "turn_off", [fan], {}))
        done = []
        ctx = replace(self._ctx, source="playbook", reason=f"draaiboek: {ent.name} detecteert "
                      f"{SAFETY_LABELS.get(ent.device_class or '', 'een alarm')}")
        for domain, service, entity_ids, data in steps:
            entity_ids = [e for e in entity_ids if not is_security_entity(index, e)]
            if not entity_ids:
                continue
            previous = ctx.autonomy.snapshot(entity_ids) if ctx.autonomy else {}
            result = await execute(ctx, domain, service, entity_ids, data)
            if result.get("status") == "ok":
                action_id = ctx.db.add_action(domain, service, entity_ids, data, previous, ctx.reason)
                description = describe_service(domain, service, entity_ids, data)
                done.append(description)
                if ctx.notifier is not None:
                    await ctx.notifier.action_done(action_id, description, ctx.reason)
        return done

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

        description, failure, snapshot, category = "", "", None, UNKNOWN
        try:
            image = await self._ctx.ha.camera_snapshot(camera.entity_id)
            if self.snapshots is not None:
                snapshot = self.snapshots.save(camera.entity_id, image)
            category, description = await classify(
                self._ctx.llm, image, camera.name,
                "Er werd net aangebeld." if doorbell else f"De sensor '{ent.name}' ging net af.",
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Camera analysis for %s failed: %s", camera.entity_id, err)
            failure = f" (geen beeldanalyse: {str(err)[:120] or type(err).__name__})"

        if category in QUIET_CATEGORIES and not doorbell:
            # Nothing, or people passing by on the street: keep it in the history, don't disturb.
            self._ctx.db.add_event("camera", f"Detectie bij {where}", description, camera.entity_id, False)
            return
        title = "🔔 Er wordt aangebeld" if doorbell else f"📷 Detectie bij {where}"
        message = description or f"{ent.name} ging af.{failure}"
        severity = "warning" if category in (SUSPICIOUS, UNKNOWN) else "info"
        if self.agent is not None and not doorbell:
            self.agent.observe(f"{title.lstrip('📷 ')} ({ent.name}): {quote(message)}", urgent=True)
        if self.incidents is not None:
            await self.incidents.report(
                "doorbell" if doorbell else "presence",
                "Er wordt aangebeld" if doorbell else f"Detectie bij {where}",
                message, severity="info" if doorbell else severity, entity_id=camera.entity_id,
                snapshot=snapshot, image_entity=camera.entity_id, correlate_seconds=60 if doorbell else 600,
                announce=f"Er wordt aangebeld. {message}" if doorbell else None,
                respect_quiet_hours=not doorbell,
            )
            return
        await self._notifier.send(title, message, kind="camera", entity_id=camera.entity_id,
                                  image_entity=camera.entity_id,
                                  announce=f"Er wordt aangebeld. {message}" if doorbell else None,
                                  respect_quiet_hours=not doorbell)

    async def handle_notification_action(self, data: dict[str, Any]) -> None:
        """Buttons pressed in a Companion app notification."""
        action = data.get("action") or ""
        if action.startswith(ACTION_PREFIX_UNDO):
            result = await undo_action(self._ctx, int(action[len(ACTION_PREFIX_UNDO):]))
            text = "↩️ Teruggedraaid" if result.get("status") == "ok" else f"❌ {result.get('error', result.get('status'))}"
            await self._notifier.send(text, result.get("description", ""), kind="confirmation_result",
                                      respect_quiet_hours=False)
            return
        for prefix, value in ((ACTION_PREFIX_FEEDBACK_UP, 1), (ACTION_PREFIX_FEEDBACK_DOWN, -1)):
            if action.startswith(prefix):
                apply_feedback(self._ctx.db, int(action[len(prefix):]), value)
                return
        for prefix, approve in ((ACTION_PREFIX_CONFIRM, True), (ACTION_PREFIX_REJECT, False)):
            if action.startswith(prefix):
                result = await resolve_confirmation(self._ctx, action[len(prefix):], approve, via="phone")
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
            text = f"{ent.name} staat al {int(open_minutes)} minuten open terwijl {nearby[0].name} verwarmt."
            if self.agent is not None:
                self.agent.observe(text, urgent=True)
            if self.incidents is not None:
                await self.incidents.report(f"energy:{ent.entity_id}", "Raam open, verwarming aan", text,
                                            severity="info", entity_id=ent.entity_id)
                continue
            await self._notifier.send("🪟 Raam open, verwarming aan", text, kind="energy", entity_id=ent.entity_id)

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
            if self.agent is not None:
                self.agent.observe(f"{ent.name} staat open terwijl niemand thuis is ({int(away_minutes)} min)")
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


def apply_feedback(db, event_id: int, value: int) -> None:
    """👍/👎 on a notification; 👎 on an anomaly means 'this is normal' and mutes it."""
    db.set_event_feedback(event_id, value)
    event = db.get_event(event_id)
    if value < 0 and event and (event.get("ref") or "").startswith("anomaly:"):
        db.mute(event["ref"])
