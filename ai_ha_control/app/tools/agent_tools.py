"""Tools for the autonomous agent (incidents, notifications, follow-ups) and house rules for the chat."""

from __future__ import annotations

import time
from typing import Any

from .base import Tool, ToolContext

MAX_AGENT_NOTIFICATIONS_PER_HOUR = 6
MIN_MINUTES_BETWEEN_INCIDENT_NOTIFICATIONS = 15
SEVERITIES = ["info", "warning", "critical"]


async def open_incident(
    ctx: ToolContext, title: str, summary: str, severity: str = "warning", entity_id: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    existing = ctx.incidents.find(title=title)
    if existing:
        ctx.db.add_incident_event(existing["id"], summary, entity_id)
        return {"status": "updated_existing", "incident_id": existing["id"]}
    allowed = notify and _notification_allowed(ctx, None)
    incident_id = await ctx.incidents.report(
        f"agent:{title.lower()[:40]}", title, summary, severity=severity, entity_id=entity_id,
        push=allowed, correlate_seconds=0,
    )
    return {"status": "opened", "incident_id": incident_id, "notified": allowed}


async def update_incident(
    ctx: ToolContext, incident_id: int, note: str, status: str | None = None, severity: str | None = None
) -> dict[str, Any]:
    incident = ctx.db.get_incident(int(incident_id))
    if incident is None:
        return {"error": f"incident {incident_id} bestaat niet"}
    ctx.db.add_incident_event(incident["id"], note)
    if status == "resolved":
        ctx.incidents.resolve(incident["id"], "Opgelost volgens de agent")
    elif status in ("open", "monitoring") or severity in SEVERITIES:
        ctx.db.update_incident(incident["id"], status=status if status in ("open", "monitoring") else None,
                               severity=severity if severity in SEVERITIES else None)
    return {"status": "ok"}


def _notification_allowed(ctx: ToolContext, incident_id: int | None) -> bool:
    now = time.time()
    if ctx.db.count_events("agent", now - 3600) >= MAX_AGENT_NOTIFICATIONS_PER_HOUR:
        return False
    if incident_id is not None and ctx.db.count_events(
            "agent", now - MIN_MINUTES_BETWEEN_INCIDENT_NOTIFICATIONS * 60, int(incident_id)):
        return False
    return True


async def notify_user(
    ctx: ToolContext, title: str, message: str, incident_id: int | None = None, camera_entity_id: str | None = None,
) -> dict[str, Any]:
    if not _notification_allowed(ctx, incident_id):
        return {"status": "suppressed", "reason": "recent al gemeld; voeg het toe aan het incident"}
    camera = ctx.index.get(camera_entity_id) if camera_entity_id else None
    if camera is not None and ctx.index.camera_profile(camera, ctx.config.camera_profiles) == "off":
        camera = None  # no picture from a bedroom or bathroom camera on a phone
    event_id = await ctx.notifier.send(
        title, message, kind="agent", incident_id=int(incident_id) if incident_id else None,
        image_entity=camera.entity_id if camera and camera.domain == "camera" else None,
    )
    if incident_id:
        ctx.db.add_incident_event(int(incident_id), f"Gemeld: {message}")
    return {"status": "sent", "event_id": event_id}


async def schedule_followup(
    ctx: ToolContext, minutes: float, note: str, incident_id: int | None = None
) -> dict[str, Any]:
    minutes = max(1.0, min(float(minutes), 24 * 60))
    followup_id = ctx.db.add_followup(time.time() + minutes * 60, note, int(incident_id) if incident_id else None)
    return {"status": "scheduled", "followup_id": followup_id, "in_minutes": minutes}


async def add_house_rule(ctx: ToolContext, text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {"error": "lege regel"}
    return {"status": "ok", "rule_id": ctx.db.add_rule(text[:300])}


async def list_house_rules(ctx: ToolContext) -> dict[str, Any]:
    return {"rules": [f"{r['id']}: {r['text']}" + ("" if r["enabled"] else " (uit)") for r in ctx.db.rules()]}


async def remove_house_rule(ctx: ToolContext, rule_id: int) -> dict[str, Any]:
    return {"status": "ok" if ctx.db.delete_rule(int(rule_id)) else "not_found"}


AGENT_TOOLS = [
    Tool(
        "open_incident",
        "Open een incident voor iets dat aandacht verdient (de gebruiker krijgt één melding). "
        "Bestaat er al een incident hierover, gebruik dan update_incident.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Korte titel, bijv. 'Vriezer warmt op'"},
                "summary": {"type": "string", "description": "Wat er aan de hand is, 1-2 zinnen"},
                "severity": {"type": "string", "enum": SEVERITIES},
                "entity_id": {"type": "string"},
                "notify": {"type": "boolean", "description": "false = alleen vastleggen, niet melden"},
            },
            "required": ["title", "summary"],
        },
        open_incident,
    ),
    Tool(
        "update_incident",
        "Voeg een notitie toe aan een incident, of sluit het (status=resolved) als het opgelost is.",
        {
            "type": "object",
            "properties": {
                "incident_id": {"type": "integer"},
                "note": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "monitoring", "resolved"]},
                "severity": {"type": "string", "enum": SEVERITIES},
            },
            "required": ["incident_id", "note"],
        },
        update_incident,
    ),
    Tool(
        "notify_user",
        "Stuur de bewoner een melding. Alleen voor dingen die echt belangrijk of nuttig zijn.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "message": {"type": "string"},
                "incident_id": {"type": "integer"},
                "camera_entity_id": {"type": "string", "description": "Camera waarvan een beeld meegaat"},
            },
            "required": ["title", "message"],
        },
        notify_user,
    ),
    Tool(
        "schedule_followup",
        "Plan een controle: over X minuten kijk je opnieuw (bijv. of een raam inmiddels dicht is).",
        {
            "type": "object",
            "properties": {
                "minutes": {"type": "number"},
                "note": {"type": "string", "description": "Wat je dan wilt controleren"},
                "incident_id": {"type": "integer"},
            },
            "required": ["minutes", "note"],
        },
        schedule_followup,
    ),
]

HOUSE_RULE_TOOLS = [
    Tool(
        "add_house_rule",
        "Voeg een huisregel in gewone taal toe waar de autonome agent zich aan houdt, bijv. 'Doe 's nachts de "
        "buitenlampen aan bij beweging in de tuin'. Voor een drempel op één meetwaarde of status ('waarschuw als "
        "de vriezer boven -15 graden komt') gebruik je add_watch_rule; temperatuur per ruimte gaat via "
        "set_comfort_profile.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        add_house_rule,
    ),
    Tool("list_house_rules", "Toon de huisregels.", {"type": "object", "properties": {}}, list_house_rules),
    Tool(
        "remove_house_rule",
        "Verwijder een huisregel.",
        {"type": "object", "properties": {"rule_id": {"type": "integer"}}, "required": ["rule_id"]},
        remove_house_rule,
    ),
]
