"""Comfort profiles: per area what the resident wants, carried out by fixed rules instead of the LLM.

A profile says, per house mode, which temperature an area should have, which lights go on at dusk,
which fan ventilates and which covers shade. The controller checks the profiles every minute and acts
through the normal autonomy rules (levels, kill switch, overrides, shadow mode). It only acts on a change
of the desired state, so it never fights a resident who adjusted something by hand.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .agent.autonomous import area_climate
from .tools.actions import perform, queue_confirmation
from .watcher.baseline import MODE_AWAY, MODE_GUEST, MODE_HOME, MODE_SLEEP, MODE_VACATION

if TYPE_CHECKING:
    from .storage.db import Database
    from .tools.base import ToolContext

_LOGGER = logging.getLogger(__name__)

MODES = (MODE_HOME, MODE_SLEEP, MODE_AWAY, MODE_VACATION, MODE_GUEST)
FALLBACK_MODE = {MODE_GUEST: MODE_HOME, MODE_VACATION: MODE_AWAY}
MIN_STEP = 0.5             # °C: smaller differences are left alone
CO2_HIGH, CO2_OK = 1000.0, 800.0
HUMIDITY_HIGH, HUMIDITY_OK = 75.0, 65.0
SHADE_ABOVE = 24.0         # °C outside
SHADE_ELEVATION = 25.0     # degrees of sun above the horizon
SHADE_POSITION = 30
VENTILATION_NOTICE_SECONDS = 3 * 3600
ARRIVAL_HISTORY_DAYS = 28
MIN_ARRIVALS = 3
SUGGESTION_DAYS = 14
MIN_CORRECTIONS = 3


def profile_target(profile: dict[str, Any], mode: str) -> float | None:
    targets = profile.get("targets") or {}
    value = targets.get(mode)
    if value is None and mode in FALLBACK_MODE:
        value = targets.get(FALLBACK_MODE[mode])
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def save_profile(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and store a profile (from the web UI or a confirmed chat request)."""
    area = (payload.get("area") or "").strip()
    if not area:
        return {"status": "error", "error": "ruimte ontbreekt"}
    targets = {}
    for mode, value in (payload.get("targets") or {}).items():
        if mode not in MODES or value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return {"status": "error", "error": f"ongeldige temperatuur voor {mode}"}
        if not 5 <= number <= 30:
            return {"status": "error", "error": f"temperatuur voor {mode} moet tussen 5 en 30 °C liggen"}
        targets[mode] = number
    profile = {**payload, "area": area, "targets": targets,
               "preheat_minutes": max(0, min(int(payload.get("preheat_minutes") or 0), 240))}
    return {"status": "ok", "profile_id": db.save_profile(profile)}


def profile_suggestions(db: Database) -> list[dict[str, Any]]:
    """Setpoints the resident keeps choosing by hand that differ from the profile."""
    suggestions = []
    since = time.time() - SUGGESTION_DAYS * 86400
    for profile in db.profiles():
        if not profile["climate_entity"]:
            continue
        by_mode: dict[str, list[float]] = {}
        for change in db.manual_changes(since, profile["climate_entity"]):
            if change["value"] is not None:
                by_mode.setdefault(change["mode"], []).append(change["value"])
        for mode, values in by_mode.items():
            if len(values) < MIN_CORRECTIONS:
                continue
            median = statistics.median(values)
            if sum(abs(v - median) <= MIN_STEP for v in values) < MIN_CORRECTIONS:
                continue  # no consistent wish
            current = profile_target(profile, mode)
            if current is not None and abs(current - median) < MIN_STEP:
                continue
            suggestions.append({"profile_id": profile["id"], "area": profile["area"], "mode": mode,
                                "value": median, "current": current, "count": len(values)})
    return suggestions


class ComfortController:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = replace(ctx, source="autonomous", conversation_id="comfort")
        self._applied: dict[str, Any] = {}   # entity -> the desired value we last acted on
        self._done_today: set[str] = set()
        self._day = ""
        self._last_notice: dict[str, float] = {}

    @property
    def config(self):
        return self._ctx.config

    def set_timezone(self, tz: str | None) -> None:
        self._ctx = replace(self._ctx, timezone=tz)

    def _free(self, entity_ids: list[str]) -> list[str]:
        """Entities a resident did not just adjust by hand."""
        overrides = self._ctx.db.active_overrides()
        return [e for e in entity_ids if e not in overrides]

    async def _act(self, domain: str, service: str, entity_ids: list[str], data: dict[str, Any],
                   reason: str) -> dict[str, Any]:
        ctx = replace(self._ctx, reason=reason)
        return await perform(ctx, domain, service, entity_ids, data, planned=True)

    async def tick(self, mode: str, now: datetime) -> list[str]:
        """Bring the house towards the profiles; returns a description of what was done or proposed."""
        if not self.config.comfort or not self.config.autonomous:
            return []
        if now.date().isoformat() != self._day:
            self._day, self._done_today = now.date().isoformat(), set()
        done: list[str] = []
        air = area_climate(self._ctx.index)
        for profile in self._ctx.db.profiles(enabled_only=True):
            try:
                done += await self._climate(profile, mode, now)
                done += await self._dusk_lights(profile, mode)
                done += await self._ventilation(profile, air.get(profile["area"], {}))
                done += await self._shading(profile)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Comfort profile %s failed", profile.get("area"))
        return done

    # --- Temperature -------------------------------------------------------------------

    async def _climate(self, profile: dict[str, Any], mode: str, now: datetime) -> list[str]:
        eid = profile["climate_entity"]
        ent = self._ctx.index.get(eid) if eid else None
        if ent is None or ent.state in ("off", "unavailable", "unknown") or not self._free([eid]):
            return []
        target = profile_target(profile, mode)
        reason = f"comfortprofiel {profile['area']}: {mode}"
        if mode in (MODE_AWAY, MODE_VACATION) and profile.get("preheat_minutes"):
            arriving = self.arriving_soon(now, profile["preheat_minutes"])
            home_target = profile_target(profile, MODE_HOME)
            if arriving and home_target is not None:
                target, reason = home_target, f"comfortprofiel {profile['area']}: voorverwarmen ({arriving})"
        if target is None:
            return []
        current = ent.attributes.get("temperature")
        if current is not None and abs(float(current) - target) < MIN_STEP:
            self._applied[eid] = target
            return []
        if self._applied.get(eid) == target:
            return []  # already acted on (or asked for) this wish; don't fight a manual change
        self._applied[eid] = target
        result = await self._act("climate", "set_temperature", [eid], {"temperature": target}, f"{reason} = {target:g} °C")
        return [f"{ent.name} → {target:g} °C ({result.get('status')})"]

    def arriving_soon(self, now: datetime, minutes: int) -> str:
        """Why we expect someone home within the preheat time ('' when not)."""
        index = self._ctx.index
        lead = minutes
        weather = next((e for e in index.entities.values() if e.domain == "weather"), None)
        outside = weather.attributes.get("temperature") if weather else None
        if outside is not None and float(outside) < 5:
            lead = int(minutes * 1.5)  # a cold house takes longer to heat
        for ent in index.entities.values():
            if ent.domain != "sensor" or not ent.entity_id.endswith("_direction_of_travel") or ent.state != "towards":
                continue
            distance = index.get(ent.entity_id.replace("_direction_of_travel", "_distance"))
            try:
                km = float(distance.state) / (1000 if (distance.unit or "m") == "m" else 1) if distance else None
            except ValueError:
                km = None
            if km is not None and km <= self.config.preheat_distance_km:
                return f"onderweg naar huis, {km:.0f} km"
        usual = self._ctx.db.arrivals(now.weekday(), time.time() - ARRIVAL_HISTORY_DAYS * 86400)
        if len(usual) >= MIN_ARRIVALS:
            expected = int(statistics.median(usual))
            minute = now.hour * 60 + now.minute
            if 0 <= expected - minute <= lead:
                return f"meestal thuis rond {expected // 60:02d}:{expected % 60:02d}"
        return ""

    # --- Light, air and sun --------------------------------------------------------------

    async def _dusk_lights(self, profile: dict[str, Any], mode: str) -> list[str]:
        lights = [e for e in profile.get("lights") or [] if self._ctx.index.get(e)]
        sun = self._ctx.index.get("sun.sun")
        key = f"dusk:{profile['id']}"
        if not lights or sun is None or sun.state != "below_horizon" or key in self._done_today:
            return []
        if mode not in (MODE_HOME, MODE_GUEST) or self._ctx.index.anyone_home() is False:
            return []
        if sun.attributes.get("rising") is True:
            return []  # morning twilight, not dusk
        self._done_today.add(key)
        off = self._free([e for e in lights if self._ctx.index.get(e).state == "off"])
        if not off:
            return []
        result = await self._act("light", "turn_on", off, {}, f"comfortprofiel {profile['area']}: schemer")
        return [f"lampen {profile['area']} aan ({result.get('status')})"]

    async def _ventilation(self, profile: dict[str, Any], air: dict[str, float]) -> list[str]:
        co2, humidity = air.get("carbon_dioxide"), air.get("humidity")
        stale = (co2 is not None and co2 > CO2_HIGH) or (humidity is not None and humidity > HUMIDITY_HIGH)
        fresh = (co2 is None or co2 < CO2_OK) and (humidity is None or humidity < HUMIDITY_OK)
        fan = profile.get("ventilation_entity")
        what = ""
        if stale:
            what = f"CO₂ {co2:.0f} ppm" if co2 is not None and co2 > CO2_HIGH else f"luchtvochtigheid {humidity:.0f}%"
        if fan and self._ctx.index.get(fan):
            if not self._free([fan]):
                return []
            state = self._ctx.index.get(fan).state
            if stale and self._applied.get(fan) != "on" and state == "off":
                self._applied[fan] = "on"
                result = await self._act(fan.split(".", 1)[0], "turn_on", [fan], {},
                                         f"comfortprofiel {profile['area']}: {what}")
                return [f"ventilatie {profile['area']} aan ({result.get('status')})"]
            if fresh and self._applied.get(fan) == "on" and state == "on":
                self._applied[fan] = "off"
                result = await self._act(fan.split(".", 1)[0], "turn_off", [fan], {},
                                         f"comfortprofiel {profile['area']}: lucht weer goed")
                return [f"ventilatie {profile['area']} uit ({result.get('status')})"]
            return []
        if not stale or self._ctx.incidents is None:
            return []
        key = f"air:{profile['area']}"
        if time.monotonic() - self._last_notice.get(key, -1e9) < VENTILATION_NOTICE_SECONDS:
            return []
        self._last_notice[key] = time.monotonic()
        await self._ctx.incidents.report(f"comfort:{profile['area']}", f"Ventileer {profile['area']}",
                                         f"In {profile['area']} is de {what}. Zet een raam open of ventileer.",
                                         severity="info", ref=f"anomaly:comfort:{profile['area']}")
        return [f"ventilatieadvies {profile['area']}"]

    async def _shading(self, profile: dict[str, Any]) -> list[str]:
        covers = self._free([e for e in profile.get("shade_covers") or [] if self._ctx.index.get(e)])
        key = f"shade:{profile['id']}"
        if not covers or key in self._done_today:
            return []
        sun = self._ctx.index.get("sun.sun")
        weather = next((e for e in self._ctx.index.entities.values() if e.domain == "weather"), None)
        outside = weather.attributes.get("temperature") if weather else None
        if sun is None or sun.state != "above_horizon" or outside is None:
            return []
        if float(sun.attributes.get("elevation") or 0) < SHADE_ELEVATION or float(outside) < SHADE_ABOVE:
            return []
        self._done_today.add(key)
        result = await self._act("cover", "set_cover_position", covers, {"position": SHADE_POSITION},
                                 f"comfortprofiel {profile['area']}: zonwering bij {outside} °C")
        return [f"zonwering {profile['area']} ({result.get('status')})"]


# --- Chat tool: the LLM turns a wish into a profile, the resident confirms ---------------------

async def set_comfort_profile(ctx: ToolContext, area: str, climate_entity: str = "", thuis: float | None = None,
                              slapen: float | None = None, weg: float | None = None,
                              vakantie: float | None = None, preheat_minutes: int = 0) -> dict[str, Any]:
    area_name = ctx.index.resolve_area(area) or area
    existing = next((p for p in ctx.db.profiles() if p["area"].lower() == area_name.lower()), None)
    if climate_entity and ctx.index.get(climate_entity) is None:
        return {"status": "error", "error": f"entiteit '{climate_entity}' bestaat niet; gebruik search_entities"}
    if not climate_entity:
        found = ctx.index.search(area=area_name, domain="climate", limit=1)
        climate_entity = found[0].entity_id if found else (existing or {}).get("climate_entity", "")
    targets = dict((existing or {}).get("targets") or {})
    targets.update({m: v for m, v in {"thuis": thuis, "slapen": slapen, "weg": weg, "vakantie": vakantie}.items()
                    if v is not None})
    payload = {**(existing or {}), "area": area_name, "climate_entity": climate_entity, "targets": targets,
               "preheat_minutes": preheat_minutes or (existing or {}).get("preheat_minutes", 0)}
    text = ", ".join(f"{m} {v:g} °C" for m, v in targets.items())
    return await queue_confirmation(ctx, "profile", payload, f"Comfortprofiel {area_name}: {text}",
                                    "een comfortprofiel bepaalt wat de agent zelf regelt")


def comfort_tools():
    from .tools.base import Tool

    number = {"type": "number"}
    return [Tool(
        "set_comfort_profile",
        "Leg vast welke temperatuur een ruimte per huismodus moet hebben (thuis, slapen, weg, vakantie), "
        "bijv. 'ik wil 's nachts 17 graden in de slaapkamer'. De agent regelt het daarna zelf. "
        "preheat_minutes: zoveel minuten voor de verwachte thuiskomst alvast opwarmen.",
        {"type": "object", "properties": {
            "area": {"type": "string"}, "climate_entity": {"type": "string"}, "thuis": number, "slapen": number,
            "weg": number, "vakantie": number, "preheat_minutes": {"type": "integer"}},
         "required": ["area"]},
        set_comfort_profile,
    )]
