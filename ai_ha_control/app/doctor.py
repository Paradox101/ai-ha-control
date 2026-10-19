"""Home doctor: a short report on the health of the Home Assistant installation. Only reads, never changes."""

from __future__ import annotations

import logging
from typing import Any

from .tools.automations import _entity_ids_in
from .tools.base import Tool, ToolContext
from .watcher.rules import minutes_since

_LOGGER = logging.getLogger(__name__)

STALE_AUTOMATION_DAYS = 60
DEAD_ENTITY_DAYS = 3
DISK_WARN_PERCENT = 85
MAX_CONFIGS = 100   # automations whose config is read per report
MAX_ITEMS = 8


def _names(items: list[str]) -> str:
    return ", ".join(items[:MAX_ITEMS]) + (f" en nog {len(items) - MAX_ITEMS}" if len(items) > MAX_ITEMS else "")


async def health_findings(ctx: ToolContext) -> dict[str, list[str]]:
    index = ctx.index
    entities = list(index.entities.values())
    days = lambda ent: minutes_since(ent.last_changed) / 1440  # noqa: E731
    findings: dict[str, list[str]] = {}

    automations = [e for e in entities if e.domain == "automation"]
    findings["Automatiseringen met een fout"] = [e.name for e in automations if e.state == "unavailable"]
    findings["Automatiseringen die uit staan"] = [e.name for e in automations if e.state == "off"]
    stale = []
    for ent in automations:
        if ent.state != "on":
            continue
        last = ent.attributes.get("last_triggered")
        if last is None and days(ent) >= STALE_AUTOMATION_DAYS:
            stale.append(f"{ent.name} (nooit)")
        elif last is not None and minutes_since(last) / 1440 >= STALE_AUTOMATION_DAYS:
            stale.append(f"{ent.name} ({int(minutes_since(last) / 1440)} dagen)")
    findings[f"Automatiseringen al {STALE_AUTOMATION_DAYS}+ dagen niet uitgevoerd"] = stale

    broken = []
    for ent in [a for a in automations if a.attributes.get("id")][:MAX_CONFIGS]:
        try:
            config = await ctx.ha.get_automation_config(str(ent.attributes["id"]))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Automation config %s: %s", ent.entity_id, err)
            continue
        problems = []
        for eid in sorted(_entity_ids_in(config)):
            target = index.get(eid)
            if target is None:
                problems.append(f"{eid} bestaat niet")
            elif target.state == "unavailable" and days(target) >= 1:
                problems.append(f"{target.name} al {int(days(target))} dagen onbereikbaar")
        if problems:
            broken.append(f"{ent.name}: {'; '.join(problems[:3])}")
    findings["Automatiseringen die naar kapotte apparaten verwijzen"] = broken

    try:  # update entities are left out of the index (they would only distract the model): ask HA directly
        states = await ctx.ha.get_states()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read states for updates: %s", err)
        states = []
    updates = []
    for state in states:
        attrs = state.get("attributes") or {}
        if state.get("entity_id", "").startswith("update.") and state.get("state") == "on":
            updates.append(f"{attrs.get('friendly_name') or state['entity_id']} "
                           f"({attrs.get('installed_version', '?')} → {attrs.get('latest_version', '?')})")
    findings["Updates beschikbaar"] = updates
    findings[f"Al {DEAD_ENTITY_DAYS}+ dagen onbereikbaar"] = [
        e.name for e in sorted(entities, key=lambda e: e.name)
        if e.state == "unavailable" and e.domain != "automation" and days(e) >= DEAD_ENTITY_DAYS]
    disks = []
    for ent in entities:
        if ent.domain == "sensor" and "disk_use_percent" in ent.entity_id:
            try:
                if float(ent.state) >= DISK_WARN_PERCENT:
                    disks.append(f"{ent.name}: {float(ent.state):.0f}% vol")
            except ValueError:
                pass
    findings["Schijf bijna vol"] = disks
    from .watcher.scheduler import low_batteries

    findings["Batterij bijna leeg"] = low_batteries(ctx)
    return {title: items for title, items in findings.items() if items}


def format_report(findings: dict[str, list[str]]) -> str:
    if not findings:
        return "Alles gezond: geen kapotte automatiseringen, updates of dode apparaten gevonden."
    return "\n".join(f"• {title}: {_names(items)}" for title, items in findings.items())


async def home_health_check(ctx: ToolContext) -> dict[str, Any]:
    findings = await health_findings(ctx)
    return {"report": format_report(findings), "findings": {k: v[:20] for k, v in findings.items()}}


async def send_health_report(ctx: ToolContext) -> str:
    """Weekly: one notification, only when there is something to fix."""
    findings = await health_findings(ctx)
    if not findings or ctx.notifier is None:
        return ""
    text = format_report(findings)
    await ctx.notifier.send("🩺 Huisdokter", text, kind="device")
    return text


DOCTOR_TOOLS = [
    Tool(
        "home_health_check",
        "Controleer de gezondheid van Home Assistant: automatiseringen met fouten, die uit staan, al lang niet "
        "liepen of naar kapotte apparaten verwijzen, beschikbare updates, apparaten die al dagen onbereikbaar "
        "zijn, een volle schijf en lege batterijen. Alleen lezen.",
        {"type": "object", "properties": {}},
        home_health_check,
    ),
]
