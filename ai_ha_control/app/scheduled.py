"""Reminders and planned actions asked for in the chat ("herinner me om 18:00", "zet over een uur de verwarming lager")."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .policy import Decision
from .tools.actions import _validate, describe_service, perform
from .tools.base import Tool, ToolContext, as_list

_LOGGER = logging.getLogger(__name__)

MAX_DAYS_AHEAD = 30
MISSED_AFTER = 6 * 3600  # an item more than this overdue (add-on was off) is reported, not executed


def _tz(ctx: ToolContext):
    return ZoneInfo(ctx.timezone) if ctx.timezone else None


def parse_when(ctx: ToolContext, at: str | None = None, in_minutes: Any = None,
               now: datetime | None = None) -> datetime | str:
    """A moment from 'HH:MM' (next time it's that late), an ISO date-time, or minutes from now. Or an error."""
    now = now or datetime.now(_tz(ctx))
    if in_minutes not in (None, ""):
        try:
            minutes = float(in_minutes)
        except (TypeError, ValueError):
            return "in_minutes moet een getal zijn"
        if minutes <= 0:
            return "in_minutes moet groter dan 0 zijn"
        when = now + timedelta(minutes=minutes)
    elif at:
        text = str(at).strip()
        try:
            if len(text) <= 5 and ":" in text:
                hour, minute = map(int, text.split(":"))
                when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if when <= now:
                    when += timedelta(days=1)
            else:
                when = datetime.fromisoformat(text)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=now.tzinfo)
        except ValueError:
            return f"tijd '{text}' niet begrepen; gebruik UU:MM of JJJJ-MM-DDTUU:MM"
    else:
        return "geef at (UU:MM) of in_minutes"
    if when <= now:
        return "dat tijdstip is al voorbij"
    if when - now > timedelta(days=MAX_DAYS_AHEAD):
        return f"maximaal {MAX_DAYS_AHEAD} dagen vooruit"
    return when


def _when_text(ctx: ToolContext, ts: float) -> str:
    when = datetime.fromtimestamp(ts, _tz(ctx))
    today = datetime.now(_tz(ctx)).date()
    day = "vandaag" if when.date() == today else "morgen" if when.date() == today + timedelta(days=1) \
        else when.strftime("%d-%m")
    return f"{day} {when:%H:%M}"


async def remind_me(ctx: ToolContext, text: str, at: str | None = None, in_minutes: Any = None) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"status": "error", "error": "waaraan moet ik herinneren?"}
    when = parse_when(ctx, at, in_minutes)
    if isinstance(when, str):
        return {"status": "error", "error": when}
    item_id = ctx.db.add_scheduled(when.timestamp(), "reminder", text[:300], {}, ctx.conversation_id)
    return {"status": "scheduled", "id": item_id, "when": _when_text(ctx, when.timestamp())}


async def schedule_action(ctx: ToolContext, domain: str, service: str, entity_ids: Any, at: str | None = None,
                          in_minutes: Any = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    entity_ids = as_list(entity_ids)
    data = {k: v for k, v in (data or {}).items() if v is not None and k != "entity_id"}
    error = _validate(ctx, domain, service, entity_ids)
    if error:
        return {"status": "error", "error": error}
    verdict = ctx.policy.check(domain, service, entity_ids)
    if verdict.decision == Decision.DENY:
        return {"status": "denied", "reason": verdict.reason}
    when = parse_when(ctx, at, in_minutes)
    if isinstance(when, str):
        return {"status": "error", "error": when}
    description = describe_service(domain, service, entity_ids, data)
    item_id = ctx.db.add_scheduled(when.timestamp(), "action", description,
                                   {"domain": domain, "service": service, "entity_ids": entity_ids, "data": data},
                                   ctx.conversation_id)
    return {"status": "scheduled", "id": item_id, "when": _when_text(ctx, when.timestamp()),
            "note": "op dat moment gelden dezelfde regels; deuren en alarm worden dan nog gevraagd"}


async def list_scheduled(ctx: ToolContext) -> dict[str, Any]:
    return {"scheduled": [f"{i['id']}: {_when_text(ctx, i['due'])} {'⏰' if i['kind'] == 'reminder' else '⚙️'} "
                          f"{i['text']}" for i in ctx.db.scheduled()]}


async def cancel_scheduled(ctx: ToolContext, item_id: int) -> dict[str, Any]:
    return {"status": "ok" if ctx.db.cancel_scheduled(int(item_id)) else "not_found"}


async def run_due(ctx: ToolContext, now: float | None = None) -> int:
    """Called every scheduler tick: send due reminders and carry out due actions."""
    now = now or time.time()
    done = 0
    for item in ctx.db.due_scheduled(now):
        ctx.db.finish_scheduled(item["id"])
        done += 1
        notifier = ctx.notifier
        late = now - item["due"] > MISSED_AFTER
        if item["kind"] == "reminder":
            if notifier is not None:
                await notifier.send("⏰ Herinnering", item["text"] + (" (te laat: de add-on stond uit)" if late else ""),
                                    kind="reminder", respect_quiet_hours=False)
            continue
        if late:
            if notifier is not None:
                await notifier.send("⚙️ Geplande actie overgeslagen",
                                    f"{item['text']}: niet uitgevoerd, de add-on stond op dat moment uit.",
                                    kind="scheduled")
            continue
        p = item["payload"]
        run_ctx = replace(ctx, source="scheduled", conversation_id=item["conversation_id"] or "scheduled",
                          reason="geplande actie")
        try:
            result = await perform(run_ctx, p["domain"], p["service"], p["entity_ids"], p.get("data") or {})
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Scheduled action %s failed", item["id"])
            result = {"status": "error", "error": str(err)}
        status = result.get("status")
        if status == "needs_confirmation":
            continue  # the confirmation question went to the phone
        title = {"ok": "⚙️ Gepland uitgevoerd", "dry_run": "🧪 Testmodus: gepland, niet uitgevoerd"}.get(
            status, "❌ Geplande actie mislukt")
        detail = "" if status in ("ok", "dry_run") else f": {result.get('error') or result.get('reason') or status}"
        if notifier is not None:
            await notifier.send(title, item["text"] + detail, kind="scheduled")
    return done


SCHEDULE_TOOLS = [
    Tool(
        "remind_me",
        "Stuur de gebruiker later een herinnering op de telefoon, bijv. 'herinner me om 18:00 aan de vuilnisbak' "
        "(at='18:00') of 'over een half uur' (in_minutes=30).",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "waaraan herinneren"},
            "at": {"type": "string", "description": "UU:MM (eerstvolgende keer) of JJJJ-MM-DDTUU:MM"},
            "in_minutes": {"type": "number"}},
         "required": ["text"]},
        remind_me,
    ),
    Tool(
        "schedule_action",
        "Voer een Home Assistant-actie later uit, bijv. 'zet over een uur de verwarming op 18' of 'doe om 23:00 "
        "de tuinverlichting uit'. Zelfde domein/service/entity_ids/data als call_service. Op het uitvoermoment "
        "gelden dezelfde regels: deuren en alarm worden dan nog aan de gebruiker gevraagd.",
        {"type": "object", "properties": {
            "domain": {"type": "string"}, "service": {"type": "string"},
            "entity_ids": {"type": "array", "items": {"type": "string"}},
            "data": {"type": "object"},
            "at": {"type": "string", "description": "UU:MM (eerstvolgende keer) of JJJJ-MM-DDTUU:MM"},
            "in_minutes": {"type": "number"}},
         "required": ["domain", "service", "entity_ids"]},
        schedule_action,
    ),
    Tool("list_scheduled", "Toon geplande herinneringen en acties.", {"type": "object", "properties": {}},
         list_scheduled),
    Tool(
        "cancel_scheduled",
        "Annuleer een geplande herinnering of actie.",
        {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]},
        cancel_scheduled,
    ),
]
