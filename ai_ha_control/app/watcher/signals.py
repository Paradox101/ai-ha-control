"""House signals the other rules don't cover: tampering while the alarm is on, a power cut (UPS on battery)
and a solar surplus worth using. Fixed rules, no LLM; only reports, never acts."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from ..house import car_devices, car_soc, grid_power, ups_on_battery, ups_runtime_minutes
from ..ha.index import EntityInfo
from ..tools.base import ToolContext
from .appliances import appliance_sensors

if TYPE_CHECKING:
    from ..notify import Notifier
    from .incidents import IncidentManager

_LOGGER = logging.getLogger(__name__)

TAMPER_CLASSES = {"vibration", "tamper"}
TAMPER_COOLDOWN = 300
SURPLUS_WATTS = 1500          # feeding back at least this much ...
SURPLUS_MINUTES = 15          # ... for this long = worth starting something
SURPLUS_HOURS = range(9, 17)  # only during the day, once a day
CAR_FULL = 90                 # % state of charge


class HouseSignals:
    def __init__(self, ctx: ToolContext, notifier: Notifier, incidents: IncidentManager | None = None) -> None:
        self._ctx = ctx
        self._notifier = notifier
        self._incidents = incidents
        self._last: dict[str, float] = {}
        self._ups: str | None = None           # entity of the UPS that is on battery
        self._surplus_since: float | None = None
        self._surplus_day = ""

    async def on_state(self, ent: EntityInfo) -> None:
        """An alarm sensor's extra contact goes off. Alarm on: vibration or tilt is a warning, an opened housing or
        a cut wire is critical (someone disabling the alarm). Alarm off: only a housing, wire or detector taken off
        its base, as a warning (👎 = changing batteries is normal)."""
        from .guard import tamper_kind

        kind = tamper_kind(ent)
        if ent.state != "on" or kind is None:
            return
        index = self._ctx.index
        armed = index.alarm_armed()
        if not armed and kind in ("vibration", "tilt"):
            return
        ref = None if armed else f"anomaly:{ent.entity_id}:tamper"
        if ref and self._ctx.db.is_muted(ref):
            return
        now = time.monotonic()
        if now - self._last.get(ent.entity_id, -1e9) < TAMPER_COOLDOWN:
            return
        self._last[ent.entity_id] = now
        what = {"vibration": "trilling", "tilt": "kanteling", "wire": "een doorgeknipt of los contact",
                "base": "dat hij van zijn voet is gehaald"}.get(kind, "sabotage (behuizing open)")
        where = f" ({ent.area})" if ent.area else ""
        text = f"{ent.device or ent.name}{where} meldt {what}" + (" terwijl het alarm aan staat." if armed else ".")
        critical = armed and kind in ("housing", "wire")
        severity = "critical" if critical else "warning"
        title = "Sabotage terwijl het alarm aan staat" if critical else (
            "Trilling of sabotage" if armed else "Sabotagemelding")
        camera = index.camera_for(ent, self._ctx.config.camera_profiles)
        if self._incidents is not None:
            await self._incidents.report("security" if armed else "tamper", title, text, severity=severity,
                                         entity_id=ent.entity_id, image_entity=camera.entity_id if camera else None,
                                         critical=critical, respect_quiet_hours=not armed, correlate_seconds=900,
                                         ref=ref)
        else:
            await self._notifier.send(f"⚠️ {title}", text, kind="security", entity_id=ent.entity_id,
                                      critical=critical, respect_quiet_hours=not armed, ref=ref)

    async def tick(self, now: datetime) -> None:
        try:
            await self._power_cut()
            if self._ctx.config.energy_context:
                await self._solar_surplus(now)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("House signals failed")

    async def _power_cut(self) -> None:
        index = self._ctx.index
        ups = ups_on_battery(index)
        if ups is not None and self._ups is None:
            self._ups = ups.entity_id
            runtime = ups_runtime_minutes(index, ups)
            text = f"{ups.device or ups.name} draait op de accu: de stroom is uitgevallen." + (
                f" Nog ongeveer {runtime} minuten." if runtime else "")
            if self._incidents is not None:
                await self._incidents.report("power", "Stroomstoring", text, severity="critical",
                                             entity_id=ups.entity_id, critical=True, respect_quiet_hours=False)
            else:
                await self._notifier.send("🔌 Stroomstoring", text, kind="device", entity_id=ups.entity_id,
                                          critical=True, respect_quiet_hours=False)
        elif ups is None and self._ups is not None:
            name = (index.get(self._ups).device if index.get(self._ups) else None) or self._ups
            self._ups = None
            await self._notifier.send("🔌 Stroom is terug", f"{name} draait weer op het net.", kind="device")

    async def _solar_surplus(self, now: datetime) -> None:
        index = self._ctx.index
        grid = grid_power(index)
        day = now.date().isoformat()
        if grid is None or now.hour not in SURPLUS_HOURS or self._surplus_day == day:
            self._surplus_since = None
            return
        if float(grid.state) > -SURPLUS_WATTS:
            self._surplus_since = None
            return
        self._surplus_since = self._surplus_since or time.time()
        if time.time() - self._surplus_since < SURPLUS_MINUTES * 60:
            return
        self._surplus_day = day
        idle = sorted({name for eid, name in appliance_sensors(index.entities.values()).items()
                       if (ent := index.get(eid)) is not None and _watts(ent) < 15})
        options = [f"de {n.lower()}" for n in idle]
        car = next((soc for ents in car_devices(index).values()
                    if (soc := car_soc(ents)) is not None and 0 <= _watts(soc) < CAR_FULL), None)
        if car is not None:
            options.append(f"de auto laden ({_watts(car):.0f}%)")
        tip = f" Goed moment voor {' of '.join(options)}." if options else ""
        await self._notifier.send("☀️ Zonne-overschot",
                                  f"Je levert al {SURPLUS_MINUTES} minuten {-float(grid.state):.0f} W terug.{tip}",
                                  kind="energy")


def _watts(ent: EntityInfo) -> float:
    try:
        return float(ent.state)
    except (TypeError, ValueError):
        return float("nan")  # unknown: neither idle nor charged
