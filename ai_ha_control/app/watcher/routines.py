"""Detect repeated manual actions (routines) from the logbook and turn them into automations."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from ..tools.automations import save_automation
from ..tools.base import ToolContext

_LOGGER = logging.getLogger(__name__)

ROUTINE_DOMAINS = ("light", "switch", "fan", "cover")
TARGET_STATES = {"on", "off", "open", "closed"}
AUTOMATED_CONTEXTS = {"automation_triggered", "script_started"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
DAYS_BACK = 14
MIN_DAYS = 5
WINDOW_MINUTES = 30


@dataclass
class Routine:
    entity_id: str
    state: str
    minutes: int
    days: int
    weekdays_only: bool

    @property
    def time(self) -> str:
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"


def _is_manual(entry: dict[str, Any]) -> bool:
    if entry.get("context_event_type") in AUTOMATED_CONTEXTS:
        return False
    return entry.get("context_domain") not in ("automation", "script")


def _when(entry: dict[str, Any]) -> datetime | None:
    when = entry.get("when")
    if isinstance(when, (int, float)):
        return datetime.fromtimestamp(when, tz=timezone.utc)
    if isinstance(when, str):
        return datetime.fromisoformat(when)
    return None


def find_routines(entries: list[dict[str, Any]], tz: ZoneInfo | None = None,
                  min_days: int = MIN_DAYS, window: int = WINDOW_MINUTES) -> list[Routine]:
    """Find (entity, state) changes that happen manually at roughly the same time on many days."""
    events: dict[tuple[str, str], list[tuple[int, Any, int]]] = defaultdict(list)
    for entry in entries:
        eid, state, when = entry.get("entity_id"), entry.get("state"), _when(entry)
        if not eid or state not in TARGET_STATES or when is None or not _is_manual(entry):
            continue
        local = when.astimezone(tz) if tz else when
        events[(eid, state)].append((local.hour * 60 + local.minute, local.date(), local.weekday()))

    routines = []
    for (eid, state), items in events.items():
        items.sort()
        best: list[tuple[int, Any, int]] = []
        for i, (start, _, _) in enumerate(items):
            cluster = [it for it in items[i:] if it[0] - start <= window]
            # one occurrence per day
            per_day = {it[1]: it for it in cluster}
            if len(per_day) > len({it[1] for it in best}):
                best = list(per_day.values())
        if len(best) < min_days:
            continue
        minute = int(round(median(it[0] for it in best) / 5) * 5) % (24 * 60)
        weekdays_only = all(it[2] < 5 for it in best)
        routines.append(Routine(eid, state, minute, len(best), weekdays_only))
    return sorted(routines, key=lambda r: (r.entity_id, r.minutes))


def describe(ctx: ToolContext, routine: Routine) -> str:
    ent = ctx.index.get(routine.entity_id)
    name = ent.name if ent else routine.entity_id
    action = {"on": "aan", "off": "uit", "open": "open", "closed": "dicht"}[routine.state]
    when = "doordeweeks " if routine.weekdays_only else ""
    return (f"Je zet {name} {when}vaak {action} rond {routine.time} "
            f"({routine.days} van de laatste {DAYS_BACK} dagen). Wil je dat automatisch laten gebeuren?")


async def analyze_routines(ctx: ToolContext) -> int:
    """Scan the logbook and store new suggestions. Returns the number of new suggestions."""
    entity_ids = [e.entity_id for e in ctx.index.entities.values() if e.domain in ROUTINE_DOMAINS]
    if not entity_ids:
        return 0
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS_BACK)
    entries = await ctx.ha.logbook(entity_ids, start.isoformat(), end.isoformat())
    tz = ZoneInfo(ctx.timezone) if ctx.timezone else None
    new = 0
    for routine in find_routines(entries or [], tz):
        if ctx.db.add_suggestion(routine.entity_id, routine.state, routine.time, routine.weekdays_only,
                                 routine.days, describe(ctx, routine)):
            new += 1
    _LOGGER.info("Routine analysis: %d new suggestions", new)
    return new


def automation_for(suggestion: dict[str, Any], name: str) -> dict[str, Any]:
    entity_id, state = suggestion["entity_id"], suggestion["state"]
    domain = entity_id.split(".", 1)[0]
    if domain == "cover":
        service = "cover.open_cover" if state == "open" else "cover.close_cover"
    else:
        service = f"{domain}.turn_on" if state == "on" else f"{domain}.turn_off"
    conditions = [{"condition": "time", "weekday": WEEKDAYS}] if suggestion["weekdays_only"] else []
    action = {"on": "aan", "off": "uit", "open": "open", "closed": "dicht"}[state]
    return {
        "alias": f"{name} {action} om {suggestion['time']}",
        "description": f"Routine herkend door AI HA Control: {suggestion['description']}",
        "triggers": [{"trigger": "time", "at": f"{suggestion['time']}:00"}],
        "conditions": conditions,
        "actions": [{"action": service, "target": {"entity_id": entity_id}}],
        "mode": "single",
    }


async def accept_suggestion(ctx: ToolContext, suggestion_id: int) -> dict[str, Any]:
    suggestion = ctx.db.get_suggestion(suggestion_id)
    if suggestion is None:
        return {"status": "error", "error": "suggestie niet gevonden"}
    ent = ctx.index.get(suggestion["entity_id"])
    config = automation_for(suggestion, ent.name if ent else suggestion["entity_id"])
    result = await save_automation(ctx, config, confirmed=True)
    if result.get("status") in ("ok", "dry_run"):
        ctx.db.set_suggestion_status(suggestion_id, "accepted" if result["status"] == "ok" else "new")
    return result
