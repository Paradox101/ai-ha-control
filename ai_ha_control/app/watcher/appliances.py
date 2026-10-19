"""Appliances that finish (washer, dryer, dishwasher by their power use) and water that never stops running."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ha.index import EntityInfo
from ..tools.base import ToolContext
from .baseline import MODE_AWAY, MODE_SLEEP, MODE_VACATION

if TYPE_CHECKING:
    from ..notify import Notifier
    from .incidents import IncidentManager

APPLIANCE_WORDS = {
    "wasmachine": "Wasmachine", "washer": "Wasmachine", "washing": "Wasmachine", "wasautomaat": "Wasmachine",
    "droger": "Droger", "dryer": "Droger", "vaatwasser": "Vaatwasser", "vaatwas": "Vaatwasser",
    "dishwasher": "Vaatwasser",
}
APPLIANCE_ICON = {"Wasmachine": "🧺", "Droger": "🧺", "Vaatwasser": "🍽️"}
RUNNING_WATTS = 15     # above this the program runs
IDLE_WATTS = 5         # below this it may be done
MIN_RUN_SECONDS = 10 * 60
DONE_AFTER_SECONDS = 10 * 60   # low power this long = finished (washers pause while soaking)
REST_SECONDS = 15 * 60         # a water meter standing still this long = no leak
QUIET_MODES = (MODE_SLEEP, MODE_AWAY, MODE_VACATION)
DAYTIME_FACTOR = 3             # during the day long showers/garden watering are normal: wait longer
PHASE_WORDS = {"fase", "phase", "l1", "l2", "l3"}


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def appliance_name(ent: EntityInfo) -> str | None:
    if ent.domain != "sensor" or ent.device_class != "power":
        return None
    text = f"{ent.entity_id} {ent.name}".lower()
    return next((name for word, name in APPLIANCE_WORDS.items() if word in text), None)


def appliance_sensors(entities, quiet: list[str] | None = None) -> dict[str, str]:
    """entity_id -> appliance name, one power sensor per device (a meter often also reports per phase).

    Appliances in `quiet` are left out: the resident's own automation already says they are done.
    """
    skip = {q.strip().lower() for q in quiet or []}
    best: dict[str, tuple[tuple[bool, str], str, str]] = {}
    for ent in entities:
        name = appliance_name(ent)
        if name is None or name.lower() in skip or any(word in ent.entity_id for word in skip if word):
            continue
        rank = (bool(set(ent.entity_id.split(".", 1)[1].split("_")) & PHASE_WORDS), ent.entity_id)
        key = ent.device_id or ent.entity_id
        if key not in best or rank < best[key][0]:
            best[key] = (rank, ent.entity_id, name)
    return {eid: name for _, eid, name in best.values()}


@dataclass
class _Run:
    started: float | None = None
    low_since: float | None = None


@dataclass
class _Water:
    value: float | None = None
    changed: float = 0.0
    rest: float = 0.0
    reported: bool = False


class ApplianceMonitor:
    def __init__(self, ctx: ToolContext, notifier: Notifier, incidents: IncidentManager | None = None) -> None:
        self._ctx = ctx
        self._notifier = notifier
        self._incidents = incidents
        self._runs: dict[str, _Run] = {}
        self._water: dict[str, _Water] = {}

    async def tick(self, mode: str, now: float | None = None) -> None:
        now = now or time.time()
        config = self._ctx.config
        entities = list(self._ctx.index.entities.values())
        appliances = appliance_sensors(entities, config.appliance_quiet) if config.appliance_alerts else {}
        for ent in entities:
            if ent.entity_id in appliances:
                await self._appliance(ent, appliances[ent.entity_id], now)
            elif appliance_name(ent) is None and config.leak_flow_hours > 0 and ent.domain == "sensor" and (
                    ent.device_class == "volume_flow_rate"
                    or (ent.device_class == "water" and ent.attributes.get("state_class") in
                        ("total", "total_increasing"))):
                await self._water_use(ent, mode, now)

    async def _appliance(self, ent: EntityInfo, name: str, now: float) -> None:
        watts = _float(ent.state)
        if watts is None:
            return
        run = self._runs.setdefault(ent.entity_id, _Run())
        if watts >= RUNNING_WATTS:
            run.started = run.started or now
            run.low_since = None
            return
        if run.started is None or watts > IDLE_WATTS:
            return
        run.low_since = run.low_since or now
        if now - run.low_since < DONE_AFTER_SECONDS:
            return
        ran = run.low_since - run.started
        self._runs[ent.entity_id] = _Run()
        if ran < MIN_RUN_SECONDS:
            return
        await self._notifier.send(f"{APPLIANCE_ICON.get(name, '✅')} {name} is klaar",
                                  f"{ent.name}: klaar na {int(ran / 60)} minuten.", kind="appliance",
                                  entity_id=ent.entity_id)

    async def _water_use(self, ent: EntityInfo, mode: str, now: float) -> None:
        value = _float(ent.state)
        if value is None:
            return
        water = self._water.get(ent.entity_id)
        if water is None:
            self._water[ent.entity_id] = _Water(value, now, now)
            return
        if ent.device_class == "volume_flow_rate":
            flowing = value > 0
        else:
            flowing = value != water.value
        if flowing:
            water.changed = now
        elif now - water.changed >= REST_SECONDS or ent.device_class == "volume_flow_rate":
            water.rest = now
            water.reported = False
        water.value = value
        hours = self._ctx.config.leak_flow_hours * (1 if mode in QUIET_MODES else DAYTIME_FACTOR)
        if water.reported or now - water.rest < hours * 3600:
            return
        water.reported = True
        text = (f"{ent.name}: er loopt al {int((now - water.rest) / 3600)} uur zonder onderbreking water. "
                "Lekkende kraan, wc of leiding?")
        if self._incidents is not None:
            await self._incidents.report(f"leak:{ent.entity_id}", "Water blijft lopen", text, severity="warning",
                                         entity_id=ent.entity_id, correlate_seconds=12 * 3600)
        else:
            await self._notifier.send("💧 Water blijft lopen", text, kind="device", entity_id=ent.entity_id)
