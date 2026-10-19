"""Presence simulation on vacation: lights go on and off in the evening the way the house is normally used."""

from __future__ import annotations

import logging
import random
from dataclasses import replace
from datetime import datetime, timedelta

from ..ha.index import DETECTION_CLASSES, EntityInfo
from ..security import is_security_entity
from ..tools.actions import execute
from ..tools.base import ToolContext
from .baseline import MODE_VACATION

_LOGGER = logging.getLogger(__name__)

LIVING_WORDS = ("woonkamer", "keuken", "living", "kitchen", "eetkamer", "zitkamer")
BUSY_SHARE = 0.3        # an hour with at least this share of the busiest hour's activity counts as "in use"
SKIP_CHANCE = 0.15      # sometimes skip an hour, so the pattern isn't the same every evening
LAST_HOUR = 23          # everything off after midnight
FALLBACK_HOURS = range(18, 23)


def _seeded(*parts) -> random.Random:
    return random.Random("|".join(str(p) for p in parts))


class PresenceSimulator:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = replace(ctx, source="simulation", conversation_id="simulation",
                            reason="aanwezigheidssimulatie (vakantie)")
        self._on: set[str] = set()  # lights the simulation turned on

    def _dark(self, now: datetime) -> bool:
        sun = self._ctx.index.get("sun.sun")
        if sun is not None:
            return sun.state == "below_horizon"
        return now.hour >= 18 or now.hour < 6

    def areas(self) -> dict[str, list[EntityInfo]]:
        index = self._ctx.index
        lights: dict[str, list[EntityInfo]] = {}
        for ent in index.entities.values():
            if ent.domain == "light" and ent.area and ent.state != "unavailable" \
                    and not is_security_entity(index, ent.entity_id):
                lights.setdefault(ent.area, []).append(ent)
        return {area: sorted(items, key=lambda e: e.entity_id) for area, items in lights.items()}

    def in_use(self, area: str, now: datetime) -> bool:
        """Is this room normally used at this hour? From the learned motion activity, else living rooms 18-23."""
        sensors = [e.entity_id for e in self._ctx.index.entities.values()
                   if e.area == area and e.domain == "binary_sensor" and e.device_class in DETECTION_CLASSES]
        rnd = _seeded(area, now.date(), now.hour)
        if rnd.random() < SKIP_CHANCE:
            return False
        hours = self._ctx.db.activity_by_hour(sensors) if sensors else {}
        if any(hours.values()):
            return hours.get(now.hour, 0) >= BUSY_SHARE * max(hours.values())
        return any(w in area.lower() for w in LIVING_WORDS) and now.hour in FALLBACK_HOURS

    def wanted(self, area: str, now: datetime) -> bool:
        # Switch at a random minute of the hour instead of exactly on the hour.
        offset = _seeded(area, now.date(), now.hour, "minute").randint(0, 25)
        moment = now if now.minute >= offset else now - timedelta(hours=1)
        if moment.hour > LAST_HOUR or moment.hour < 12 or not self._dark(now):
            return False
        return self.in_use(area, moment)

    async def tick(self, mode: str, now: datetime) -> None:
        config, autonomy = self._ctx.config, self._ctx.autonomy
        active = config.presence_simulation and mode == MODE_VACATION and (autonomy is None or autonomy.enabled)
        if not active:
            self._on.clear()  # back home or switched off: leave the lights to the residents
            return
        for area, lights in self.areas().items():
            ours = [e for e in lights if e.entity_id in self._on]
            if self.wanted(area, now):
                if not ours and not any(e.state == "on" for e in lights):
                    await self._switch("turn_on", lights[0])
            else:
                for ent in ours:
                    if ent.state == "on":
                        await self._switch("turn_off", ent)
                    self._on.discard(ent.entity_id)

    async def _switch(self, service: str, ent: EntityInfo) -> None:
        result = await execute(self._ctx, "light", service, [ent.entity_id], {})
        if result.get("status") in ("ok", "dry_run") and service == "turn_on":
            self._on.add(ent.entity_id)
        _LOGGER.info("Presence simulation: %s %s (%s)", service, ent.entity_id, result.get("status"))
