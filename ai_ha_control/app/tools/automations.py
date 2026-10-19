"""Tools for automations, long-term memory and recent events."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .actions import queue_confirmation
from .base import Tool, ToolContext, as_list


def _entity_ids_in(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "entity_id":
                found.update(e for e in as_list(value) if "." in e)
            else:
                found |= _entity_ids_in(value)
    elif isinstance(obj, list):
        for item in obj:
            found |= _entity_ids_in(item)
    return found


async def save_automation(ctx: ToolContext, config: dict[str, Any], confirmed: bool = False) -> dict[str, Any]:
    config = dict(config)
    automation_id = str(config.pop("id", None) or int(time.time() * 1000))
    audit_args = {"id": automation_id, "config": config}
    if ctx.config.dry_run:
        ctx.db.audit("automation", audit_args, "dry_run", None, ctx.conversation_id)
        return {"status": "dry_run", "message": "Testmodus: automatisering is NIET aangemaakt."}
    try:
        await ctx.ha.save_automation_config(automation_id, config)
    except Exception as err:  # noqa: BLE001
        ctx.db.audit("automation", audit_args, "failed", str(err), ctx.conversation_id)
        return {"status": "error", "error": str(err)}
    ctx.db.audit("automation", audit_args, "confirmed" if confirmed else "executed", "ok", ctx.conversation_id)
    return {"status": "ok", "automation_id": automation_id, "alias": config.get("alias")}


def _local_time(ctx: ToolContext, iso: str) -> str:
    """Home Assistant gives last_triggered in UTC; the resident thinks in local time."""
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(ctx.timezone) if ctx.timezone else None) \
            .strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso)[:16]


async def list_automations(ctx: ToolContext) -> dict[str, Any]:
    items = []
    for ent in ctx.index.search(domain="automation", limit=200):
        last = ent.attributes.get("last_triggered")
        items.append(f"{ent.entity_id} ({ent.name}) = {ent.state}" + (f", laatst: {_local_time(ctx, last)}" if last else ""))
    return {"automations": items}


async def get_automation(ctx: ToolContext, entity_id: str) -> dict[str, Any]:
    ent = ctx.index.get(entity_id)
    if ent is None or ent.domain != "automation":
        return {"error": f"'{entity_id}' is geen automatisering"}
    automation_id = ent.attributes.get("id")
    if not automation_id:
        return {"error": "deze automatisering staat niet in automations.yaml en kan niet worden gelezen"}
    return {"id": automation_id, "config": await ctx.ha.get_automation_config(str(automation_id))}


async def related_automations(ctx: ToolContext, entity_id: str) -> dict[str, Any]:
    """Automations and scripts that use (trigger on, check or control) an entity."""
    if ctx.index.get(entity_id) is None:
        return {"error": f"entiteit '{entity_id}' bestaat niet; gebruik search_entities"}
    try:
        related = await ctx.ha.related("entity", entity_id)
    except Exception as err:  # noqa: BLE001
        return {"error": f"zoeken in Home Assistant mislukt: {err}"}

    def describe(ids: list[str]) -> list[str]:
        out = []
        for eid in ids or []:
            ent = ctx.index.get(eid)
            last = ent.attributes.get("last_triggered") if ent else None
            out.append(f"{eid} ({ent.name if ent else eid})" + (f", laatst: {_local_time(ctx, last)}" if last else ""))
        return out

    return {"automations": describe(related.get("automation", [])), "scripts": describe(related.get("script", [])),
            "scenes": describe(related.get("scene", []))}


async def create_automation(
    ctx: ToolContext,
    alias: str,
    triggers: Any,
    actions: Any,
    conditions: Any = None,
    description: str = "",
    mode: str = "single",
) -> dict[str, Any]:
    def as_items(value: Any) -> list[Any]:
        if isinstance(value, str):
            value = json.loads(value)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    try:
        triggers, actions, conditions = as_items(triggers), as_items(actions), as_items(conditions)
    except json.JSONDecodeError:
        return {"error": "triggers/actions/conditions moeten JSON-objecten zijn"}
    if not triggers or not actions:
        return {"error": "minstens één trigger en één actie nodig"}

    unknown = sorted(e for e in _entity_ids_in([triggers, conditions, actions]) if ctx.index.get(e) is None)
    if unknown:
        return {"error": f"onbekende entiteiten: {unknown}. Gebruik search_entities."}

    try:
        validation = await ctx.ha.validate_config(triggers, conditions, actions)
    except Exception as err:  # noqa: BLE001
        validation = {"_": {"valid": True, "error": f"validatie niet beschikbaar: {err}"}}
    errors = {k: v.get("error") for k, v in (validation or {}).items() if isinstance(v, dict) and not v.get("valid")}
    if errors:
        return {"error": "ongeldige automatisering", "details": errors}

    config = {
        "alias": alias,
        "description": description or "Aangemaakt door AI HA Control",
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
        "mode": mode,
    }
    return await queue_confirmation(
        ctx,
        "automation",
        {"config": config},
        f"Nieuwe automatisering '{alias}': {json.dumps(config, ensure_ascii=False)[:600]}",
        "nieuwe automatiseringen vereisen altijd bevestiging",
    )


async def remember(ctx: ToolContext, fact: str) -> dict[str, Any]:
    fact = fact.strip()
    if not fact:
        return {"error": "leeg feit"}
    return {"status": "ok", "memory_id": ctx.db.add_memory(fact[:500])}


async def forget(ctx: ToolContext, memory_id: int) -> dict[str, Any]:
    return {"status": "ok" if ctx.db.delete_memory(int(memory_id)) else "not_found"}


async def get_recent_events(ctx: ToolContext, hours: float = 24) -> dict[str, Any]:
    since = time.time() - float(hours) * 3600
    tz = ZoneInfo(ctx.timezone) if ctx.timezone else None
    events = [
        f"{datetime.fromtimestamp(e['ts'], tz):%d-%m %H:%M} [{e['kind']}] {e['title']}: {e['message']}"
        for e in ctx.db.events(since=since, limit=40)
    ]
    actions = [
        f"{datetime.fromtimestamp(a['ts'], tz):%d-%m %H:%M} {a['action']} {a['decision']}: "
        f"{json.dumps(a['arguments'], ensure_ascii=False)[:150]}"
        for a in ctx.db.audit_actions(since, exclude=("assist", "settings"), limit=20)
    ]
    return {"events": events, "ai_actions": actions}


OBJECT_LIST = {"type": "array", "items": {"type": "object"}}

TOOLS = [
    Tool(
        "list_automations",
        "Lijst van alle automatiseringen met status en laatste keer uitgevoerd.",
        {"type": "object", "properties": {}},
        list_automations,
    ),
    Tool(
        "get_automation",
        "De volledige configuratie van één automatisering.",
        {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]},
        get_automation,
    ),
    Tool(
        "related_automations",
        "Welke automatiseringen, scripts en scènes een entiteit gebruiken of aansturen, bijv. wat 'scene.afwezig' "
        "activeert of waarom een lamp aanging.",
        {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]},
        related_automations,
    ),
    Tool(
        "create_automation",
        "Maak een nieuwe Home Assistant automatisering (de gebruiker moet bevestigen). Gebruik HA-formaat, bijv. "
        "triggers=[{'trigger':'state','entity_id':'binary_sensor.gang_beweging','to':'on'}], "
        "conditions=[{'condition':'time','after':'23:00:00','before':'07:00:00'}], "
        "actions=[{'action':'light.turn_on','target':{'entity_id':'light.gang'},'data':{'brightness_pct':10}}].",
        {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Naam van de automatisering"},
                "description": {"type": "string"},
                "triggers": OBJECT_LIST,
                "conditions": OBJECT_LIST,
                "actions": OBJECT_LIST,
                "mode": {"type": "string", "enum": ["single", "restart", "queued", "parallel"]},
            },
            "required": ["alias", "triggers", "actions"],
        },
        create_automation,
    ),
    Tool(
        "remember",
        "Onthoud een blijvend feit of voorkeur van de bewoners, bijv. 'Vincent wil 19,5 graden in de woonkamer'.",
        {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
        remember,
    ),
    Tool(
        "forget",
        "Vergeet een eerder onthouden feit (memory_id staat in de systeemprompt).",
        {"type": "object", "properties": {"memory_id": {"type": "integer"}}, "required": ["memory_id"]},
        forget,
    ),
    Tool(
        "get_recent_events",
        "Wat er recent gebeurd is: meldingen van de AI (beweging, camera, ramen, veiligheid) en uitgevoerde acties.",
        {"type": "object", "properties": {"hours": {"type": "number", "description": "Aantal uur terug"}}},
        get_recent_events,
    ),
]
