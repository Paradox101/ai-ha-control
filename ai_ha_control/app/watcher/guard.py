"""Home security over more than one sensor: the alarm switched on with a window open, forgotten when everybody
left, cameras or the alarm's own connection gone while it is on, one person seen at several cameras, and a
weekly report. Fixed rules, no LLM. Only ever reports: the agent never arms, disarms or unlocks anything."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..ha.index import normalize
from ..house import is_pseudo_camera, ups_on_battery
from ..house import label as house_label
from ..house import words as house_words
from .baseline import MODE_AWAY, MODE_VACATION, house_mode
from .rules import is_unsecured, is_vehicle, minutes_since, quote

if TYPE_CHECKING:
    from ..ha.index import EntityIndex, EntityInfo
    from ..notify import Notifier
    from ..tools.base import ToolContext
    from .incidents import IncidentManager

_LOGGER = logging.getLogger(__name__)

ACTIVE_ALARM = ("armed", "arming", "pending", "triggered")   # state prefixes: the alarm is on (or going on)
CAMERA_OFFLINE_MINUTES = 10
LINK_OFFLINE_MINUTES = 5
ROUTE_SECONDS = 600          # sightings at different cameras within this time are one person walking around
ROUTE_COOLDOWN = 900
REMINDER_REF = "anomaly:alarm:off-away"   # 👎 on the reminder = "I never use the alarm when I leave"
REPORT_DAYS = 7
SECURITY_INCIDENT_KINDS = {"security", "tamper", "alarm_link", "camera_offline", "armed_open"}
LINK_WORDS = {"meldkamer", "alarmcentrale", "monitoring"}
DOOR_CLASSES = {"door", "garage_door", "opening"}


def alarm_panels(index: EntityIndex) -> list[EntityInfo]:
    return [e for e in index.entities.values() if e.domain == "alarm_control_panel"]


def alarm_active(ent: EntityInfo) -> bool:
    return ent.state.startswith(ACTIVE_ALARM)


def _is_alarm_word(token: str) -> bool:
    return token.endswith("alarm") or token in LINK_WORDS or token == "alarmsysteem"


def alarm_links(index: EntityIndex) -> list[EntityInfo]:
    """The alarm's own connections (to the monitoring centre, mobile network): connectivity sensors on the alarm
    panel's device or named after the alarm."""
    devices = {p.device_id for p in alarm_panels(index) if p.device_id}
    return [e for e in index.entities.values()
            if e.domain == "binary_sensor" and e.device_class == "connectivity"
            and ((e.device_id and e.device_id in devices) or any(_is_alarm_word(w) for w in house_words(e)))]


def tamper_kind(ent: EntityInfo) -> str | None:
    """What an alarm sensor's extra contact means: housing opened, wire cut, taken off its base, tilt, vibration."""
    if ent.domain != "binary_sensor":
        return None
    w = house_words(ent)
    if ent.device_class == "vibration" or w & {"vibratie", "trilling", "vibration"}:
        return "vibration"
    if w & {"kanteling", "tilt"}:
        return "tilt"
    if ent.device_class == "problem" and "verbroken" in w and "contact" in w:
        return "wire"
    if w & {"removed", "verwijderd"} and w & {"base", "basis"}:
        return "base"
    if ent.device_class == "tamper" or w & {"sabotage", "tamper", "deksel"}:
        return "housing"
    return None


def physical_cameras(index: EntityIndex, profiles: list[str]) -> dict[str, list[EntityInfo]]:
    """Real cameras (no mail or map pictures, not set to "ignore"), with all their streams."""
    cams: dict[str, list[EntityInfo]] = {}
    for cam in index.entities.values():
        if cam.domain != "camera" or is_pseudo_camera(cam) or index.camera_profile(cam, profiles) == "off":
            continue
        cams.setdefault(index._camera_key(cam), []).append(cam)
    return cams


def offline_cameras(index: EntityIndex, profiles: list[str]) -> list[str]:
    return sorted(index.camera_label(streams[0]) for streams in physical_cameras(index, profiles).values()
                  if all(c.state == "unavailable" for c in streams))


def _names(items: list[str], limit: int = 8) -> str:
    return ", ".join(items[:limit]) + (f" en nog {len(items) - limit}" if len(items) > limit else "")


def _where(ent: EntityInfo) -> str:
    return house_label(ent) + (f" ({ent.area})" if ent.area else "")


def open_now(index: EntityIndex, doors_only: bool = False) -> list[EntityInfo]:
    """Open doors/windows and unlocked doors of the house (not the car)."""
    found = []
    for ent in index.entities.values():
        if is_vehicle(ent):
            continue
        if ent.state == "on" and index.is_opening(ent):
            if doors_only and ent.device_class not in DOOR_CLASSES:
                continue
            found.append(ent)
        elif is_unsecured(ent):
            found.append(ent)
    return found


def security_devices(index: EntityIndex) -> set[str]:
    """Devices that guard the house: alarm sensors, sirens, panels, locks, smoke detectors."""
    devices = set()
    for ent in index.entities.values():
        if not ent.device_id:
            continue
        if ent.domain in ("alarm_control_panel", "siren", "lock") or tamper_kind(ent) \
                or ent.device_class in ("safety", "smoke", "carbon_monoxide", "gas", "moisture"):
            devices.add(ent.device_id)
    return devices


def security_batteries(index: EntityIndex, threshold: float = 20) -> list[str]:
    from ..house import rechargeable_devices

    devices, low, seen = security_devices(index) - rechargeable_devices(index), [], set()
    for ent in index.entities.values():
        if ent.device_class != "battery" or ent.domain != "sensor" or ent.device_id not in devices \
                or ent.device_id in seen:
            continue
        try:
            level = float(ent.state)
        except ValueError:
            continue
        if level <= threshold:
            seen.add(ent.device_id)
            low.append(f"{ent.device or ent.name} ({level:.0f}%)")
    return sorted(low)


def security_status(ctx: ToolContext) -> dict[str, Any]:
    """Live state of everything that guards the house, for the chat and the weekly report."""
    index, profiles = ctx.index, ctx.config.camera_profiles
    return {
        "alarm": [f"{p.name}: {p.readable_state}" for p in alarm_panels(index)],
        "open": [_where(e) for e in open_now(index)],
        "cameras_offline": offline_cameras(index, profiles),
        "alarm_connection_lost": [e.name for e in alarm_links(index) if e.state == "off"],
        "tamper_active": sorted(_where(e) for e in index.entities.values()
                                if e.state == "on" and tamper_kind(e) in ("housing", "wire", "base")),
        "low_batteries": security_batteries(index),
        "power_cut": bool(ups_on_battery(index)),
    }


SECURITY_QUESTION_WORDS = {"alarm", "inbraakalarm", "alarmsysteem", "beveiliging", "beveiligd", "beveiligingsstatus",
                           "security", "secured"}
NOT_A_SECURITY_QUESTION = {"zet", "doe", "schakel", "activeer", "deactiveer", "wanneer", "gisteren", "vandaag",
                           "vaak", "keer", "afgegaan", "automatisering", "automation", "arm", "disarm", "when",
                           "camera", "gezien", "wekker", "rookmelder", "timer"}
QUESTION_START = {"hoe", "is", "staat", "staan", "wat", "zijn", "how", "what", "are", "ben", "geef"}


def security_answer(ctx: ToolContext, question: str) -> str | None:
    """"Staat het alarm aan?" answered straight from the panels and sensors; None for anything else."""
    tokens = normalize(question).split()
    if not tokens or not set(tokens) & SECURITY_QUESTION_WORDS or set(tokens) & NOT_A_SECURITY_QUESTION:
        return None
    if "?" not in question and tokens[0] not in QUESTION_START:
        return None
    if not alarm_panels(ctx.index):
        return None
    status = security_status(ctx)
    sentences = [f"{line}." for line in status["alarm"]]
    sentences.append(f"Open of niet op slot: {_names(status['open'])}." if status["open"]
                     else "Alle deuren en ramen zijn dicht.")
    if status["cameras_offline"]:
        sentences.append(f"Camera offline: {_names(status['cameras_offline'])}.")
    if status["alarm_connection_lost"]:
        sentences.append(f"Geen verbinding: {_names(status['alarm_connection_lost'])}.")
    if status["tamper_active"]:
        sentences.append(f"Sabotagemelding actief: {_names(status['tamper_active'])}.")
    if status["power_cut"]:
        sentences.append("De stroom is uitgevallen (UPS op accu).")
    return " ".join(sentences)


class SecurityGuard:
    def __init__(self, ctx: ToolContext, notifier: Notifier, incidents: IncidentManager | None = None) -> None:
        self._ctx = ctx
        self._notifier = notifier
        self._incidents = incidents
        self._last: dict[str, float] = {}
        self._camera_up: set[str] = set()       # cameras seen working: an always-dead one is no news
        self._camera_reported: set[str] = set()
        self._link_up: set[str] = set()
        self._link_reported: set[str] = set()
        self._was_armed: set[str] = set()       # panels that were on when they went unreachable
        self._sightings: list[tuple[float, str, str, str]] = []  # (time, camera key, label, profile)
        self._reminded: str | None = None       # departure the "alarm is off" reminder was sent for
        self.on_observation = None              # callback(text, urgent) to inform the autonomous agent

    def _now(self) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(self._ctx.timezone) if self._ctx.timezone else None)

    def _allowed(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < seconds:
            return False
        self._last[key] = now
        return True

    def _observe(self, text: str, urgent: bool = False) -> None:
        if self.on_observation:
            self.on_observation(f"BEVEILIGING: {text}", urgent)

    async def _report(self, kind: str, title: str, text: str, severity: str = "warning", critical: bool = False,
                      entity_id: str | None = None, **extra: Any) -> None:
        if self._incidents is not None:
            await self._incidents.report(kind, title, text, severity=severity, entity_id=entity_id,
                                         critical=critical, respect_quiet_hours=False, **extra)
            return
        extra.pop("correlate_seconds", None)
        extra.pop("snapshot", None)
        await self._notifier.send(f"🛡️ {title}", text, kind="security", entity_id=entity_id, critical=critical,
                                  respect_quiet_hours=False, **extra)

    # --- Events --------------------------------------------------------------------------

    async def on_state(self, ent: EntityInfo, old: str | None, new: str | None) -> None:
        if ent.domain != "alarm_control_panel" or not new or not new.startswith("armed") \
                or (old or "").startswith("armed"):
            return
        self._reminded = "armed"  # switched on: no reminder for this departure any more
        # Away/vacation: every open window counts. Home/night: windows may be open on purpose, doors may not.
        doors_only = new in ("armed_home", "armed_night")
        found = open_now(self._ctx.index, doors_only=doors_only)
        if not found or not self._allowed("armed-open", 120):
            return
        text = f"{ent.name} staat aan, maar {_names([_where(e) for e in found])} " \
               f"{'staat' if len(found) == 1 else 'staan'} nog open of niet op slot."
        self._observe(text, urgent=True)
        await self._report("armed_open", "Alarm aan met iets open", text, entity_id=ent.entity_id,
                           correlate_seconds=300)

    async def person_seen(self, camera: EntityInfo, where: str, profile: str, description: str = "",
                          snapshot: str | None = None) -> None:
        """A person on a camera while away or armed: at several cameras within minutes = walking around the house."""
        index = self._ctx.index
        armed = index.alarm_armed()
        if not armed and house_mode(index, self._ctx.config, self._now()) not in (MODE_AWAY, MODE_VACATION):
            return
        now = time.monotonic()
        key = index._camera_key(camera)
        self._sightings = [s for s in self._sightings if now - s[0] < ROUTE_SECONDS and s[1] != key]
        self._sightings.append((now, key, where, profile))
        private = any(s[3] == "private" for s in self._sightings)
        # The doorbell and the driveway camera both see the postman: only the way to the back, or three cameras.
        if len(self._sightings) < 2 or (not private and len(self._sightings) < 3):
            return
        if not self._allowed("route", ROUTE_COOLDOWN):
            return
        route = " → ".join(s[2] for s in self._sightings)
        why = "het alarm staat aan" if armed else "niemand is thuis"
        text = f"Iemand is binnen {ROUTE_SECONDS // 60} minuten bij {len(self._sightings)} camera's gezien " \
               f"terwijl {why}: {route}." + (f" {description}" if description else "")
        self._observe(f"Iemand loopt rond het huis: {route} {quote(description)}", urgent=True)
        critical = armed and private
        await self._report("security" if critical else "presence", "Iemand loopt rond het huis", text,
                           severity="critical" if critical else "warning", critical=critical,
                           entity_id=camera.entity_id, snapshot=snapshot, image_entity=camera.entity_id,
                           correlate_seconds=900)

    # --- Periodic ------------------------------------------------------------------------

    async def tick(self, now: datetime) -> None:
        for name, rule in (("reminder", self._alarm_forgotten), ("cameras", self._cameras_offline),
                           ("links", self._links_offline)):
            try:
                await rule(now)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Security rule %s failed", name)

    async def _alarm_forgotten(self, now: datetime) -> None:
        config, index = self._ctx.config, self._ctx.index
        panels = [p for p in alarm_panels(index) if p.state not in ("unavailable", "unknown")]
        if config.alarm_reminder_minutes <= 0 or not panels or any(alarm_active(p) for p in panels):
            return
        if index.anyone_home() is not False or house_mode(index, config, now) not in (MODE_AWAY, MODE_VACATION):
            self._reminded = None
            return
        people = [e for e in index.entities.values() if e.domain == "person"]
        if not people:
            return
        away = min(minutes_since(p.last_changed) for p in people)
        left_at = max((p.last_changed or "") for p in people)
        if away < config.alarm_reminder_minutes or self._reminded in (left_at, "armed"):
            return
        self._reminded = left_at
        if self._ctx.db.is_muted(REMINDER_REF):
            return
        names = " en ".join(p.name for p in panels)
        text = f"Niemand is al {int(away)} minuten thuis en {names} staat uit. Zet het alarm aan in je alarm-app; " \
               "dat doe ik nooit zelf."
        self._observe(text)
        await self._notifier.send("🛡️ Alarm staat uit", text, kind="security", entity_id=panels[0].entity_id,
                                  respect_quiet_hours=False, ref=REMINDER_REF)

    async def _cameras_offline(self, now: datetime) -> None:
        index, config = self._ctx.index, self._ctx.config
        guarding = index.alarm_armed() or house_mode(index, config, now) in (MODE_AWAY, MODE_VACATION)
        dropped = []
        for key, streams in physical_cameras(index, config.camera_profiles).items():
            if any(c.state != "unavailable" for c in streams):
                self._camera_up.add(key)
                self._camera_reported.discard(key)
                continue
            if key not in self._camera_up or key in self._camera_reported or not guarding:
                continue
            if min(minutes_since(c.last_changed) for c in streams) < CAMERA_OFFLINE_MINUTES:
                continue
            self._camera_reported.add(key)
            dropped.append(streams[0])
        if not dropped:
            return
        why = "het alarm aan staat" if index.alarm_armed() else "niemand thuis is"
        names = _names(sorted(index.camera_label(c) for c in dropped))
        text = f"{'Camera' if len(dropped) == 1 else 'Camera’s'} {names} " \
               f"{'is' if len(dropped) == 1 else 'zijn'} al {CAMERA_OFFLINE_MINUTES} minuten offline terwijl {why}."
        if ups_on_battery(index):
            text += " De stroom is uitgevallen."
        self._observe(text, urgent=True)
        await self._report("camera_offline", "Camera offline", text, entity_id=dropped[0].entity_id,
                           correlate_seconds=3600)

    async def _links_offline(self, now: datetime) -> None:
        """The alarm lost its connection (monitoring centre, network) or Home Assistant can't reach it."""
        index = self._ctx.index
        armed = index.alarm_armed()
        lost = []
        for ent in alarm_links(index) + alarm_panels(index):
            down = ent.state == ("unavailable" if ent.domain == "alarm_control_panel" else "off")
            if not down:
                if ent.state != "unavailable":
                    self._link_up.add(ent.entity_id)
                    self._link_reported.discard(ent.entity_id)
                if ent.domain == "alarm_control_panel" and alarm_active(ent):
                    self._was_armed.add(ent.entity_id)
                elif ent.domain == "alarm_control_panel":
                    self._was_armed.discard(ent.entity_id)
                continue
            if ent.entity_id not in self._link_up or ent.entity_id in self._link_reported:
                continue
            if minutes_since(ent.last_changed) < LINK_OFFLINE_MINUTES:
                continue
            self._link_reported.add(ent.entity_id)
            lost.append(ent)
        if not lost:
            return
        on = armed or any(e.entity_id in self._was_armed for e in lost)
        text = "; ".join(f"{e.name}: {'onbereikbaar' if e.domain == 'alarm_control_panel' else 'geen verbinding'}"
                         for e in lost) + (" (het alarm staat aan)." if on else ".")
        if ups_on_battery(index):
            text += " De stroom is uitgevallen."
        self._observe(text, urgent=on)
        await self._report("alarm_link", "Alarmsysteem zonder verbinding", text, entity_id=lost[0].entity_id,
                           severity="critical" if on else "warning", critical=on, correlate_seconds=3600)

    # --- Weekly report -------------------------------------------------------------------

    def weekly_report(self) -> str:
        ctx = self._ctx
        since = time.time() - REPORT_DAYS * 86400
        incidents = [i for i in ctx.db.incidents(since=since, limit=500) if i["opened"] >= since]
        lines = []
        alarms = [i for i in incidents if i["title"] == "Alarm afgegaan"]
        tz = self._now().tzinfo
        if alarms:
            when = ", ".join(f"{datetime.fromtimestamp(i['opened'], tz):%d-%m %H:%M}" for i in alarms[:5])
            lines.append(f"🚨 Alarm afgegaan: {len(alarms)}× ({when})")
        counts: dict[str, int] = {}
        for i in incidents:
            if i["kind"] in SECURITY_INCIDENT_KINDS and i["title"] != "Alarm afgegaan":
                counts[i["title"]] = counts.get(i["title"], 0) + 1
        if counts:
            lines.append("🛡️ Meldingen: " + ", ".join(f"{t} {n}×" for t, n in sorted(counts.items())))
        people = sum(1 for i in incidents if i["kind"] == "presence")
        if people:
            lines.append(f"📷 Personen bij de camera's: {people}×")
        forgotten = sum(1 for e in ctx.db.events(since=since, limit=1000) if e.get("ref") == REMINDER_REF)
        if forgotten:
            lines.append(f"🔓 Alarm uit terwijl niemand thuis was: {forgotten}×")
        status = security_status(ctx)
        for icon, title, key in (("📷", "Nu offline", "cameras_offline"),
                                 ("📡", "Geen verbinding", "alarm_connection_lost"),
                                 ("⚠️", "Sabotagecontact actief", "tamper_active"),
                                 ("🔋", "Batterij bijna leeg", "low_batteries")):
            if status[key]:
                lines.append(f"{icon} {title}: {_names(status[key])}")
        if not lines:
            lines.append("Rustige week: geen beveiligingsmeldingen, alle camera's en het alarm zijn online.")
        if status["alarm"]:
            lines.append("Alarm nu: " + "; ".join(status["alarm"]))
        return "\n".join(lines)

    async def send_weekly_report(self) -> str:
        if not alarm_panels(self._ctx.index) and not physical_cameras(self._ctx.index, []):
            return ""  # nothing that guards the house
        text = self.weekly_report()
        await self._notifier.send("🛡️ Beveiligingsrapport", text, kind="report")
        return text


async def _security_status_tool(ctx: ToolContext) -> dict[str, Any]:
    status = security_status(ctx)
    status["week"] = SecurityGuard(ctx, ctx.notifier).weekly_report()
    return status


def security_tools() -> list:
    from ..tools.base import Tool

    return [Tool(
        "security_status",
        "Beveiliging van het huis nu: alarmstatus, open deuren/ramen en sloten, camera's offline, verbinding van "
        "het alarm, sabotagecontacten, lege batterijen van alarmsensoren, plus de meldingen van de afgelopen week. "
        "Alleen lezen; het alarm en de sloten bedien je nooit zelf.",
        {"type": "object", "properties": {}},
        _security_status_tool,
    )]
