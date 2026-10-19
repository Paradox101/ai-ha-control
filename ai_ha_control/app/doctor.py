"""Home doctor: a short report on the health of the Home Assistant installation. Only reads, never changes."""

from __future__ import annotations

import logging
from typing import Any

from .house import duplicates, house_fan, mode_helper, words
from .tools.automations import _entity_ids_in
from .tools.base import Tool, ToolContext
from .watcher.rules import minutes_since

_LOGGER = logging.getLogger(__name__)

STALE_AUTOMATION_DAYS = 60
DEAD_ENTITY_DAYS = 3
DISK_WARN_PERCENT = 85
MAX_CONFIGS = 100   # automations whose config is read per report
MAX_ITEMS = 8
DEVICE_DOMAINS = {"camera", "light", "lock", "climate", "cover", "media_player", "remote", "siren", "fan"}


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
    findings["Rook-, gas- en CO-melders"] = detector_problems(index)
    findings["Instellingen die je kunt invullen"] = setting_suggestions(ctx)
    pairs = duplicates(index)
    findings["Dubbele entiteiten (oude kopie met _2/_3, volledige lijst in de export)"] = [
        dup.entity_id for _, dup in pairs if dup.domain in DEVICE_DOMAINS or (dup.area and dup.area.lower() not in
                                                                              {a.lower() for a in ctx.config.offline_ignore_areas})]
    return {title: items for title, items in findings.items() if items}


def detector_problems(index) -> list[str]:
    """Failed self tests and problems of smoke/gas/CO detectors (a detector that fails a test may not go off)."""
    detector_devices = {e.device_id for e in index.entities.values() if e.device_id and e.domain == "binary_sensor"
                        and e.device_class in ("smoke", "gas", "carbon_monoxide")}
    problems = []
    for ent in index.entities.values():
        if ent.device_id not in detector_devices:
            continue
        if ent.domain == "binary_sensor" and ent.device_class == "problem" and ent.state == "on":
            problems.append(f"{ent.name}: probleem")
        elif ent.domain == "sensor" and "self_test" in ent.entity_id and ent.state in ("not_completed", "failed",
                                                                                        "fault"):
            problems.append(f"{ent.name}: {'zelftest nog niet gedaan' if ent.state == 'not_completed' else 'zelftest mislukt'}")
    return problems


def setting_suggestions(ctx: ToolContext) -> list[str]:
    """Options that are empty while the house has what they need."""
    index, config = ctx.index, ctx.config
    entities = list(index.entities.values())
    tips = []
    valve = next((e for e in entities if e.domain == "valve" and words(e) & {"water", "waterafsluiter", "hoofdkraan",
                                                                          "kraan"}), None)
    if not config.leak_valve and valve is not None:
        tips.append(f"Hoofdkraan bij lekkage: {valve.entity_id}")
    tts = sorted(e.entity_id for e in entities if e.domain == "tts")
    if not config.tts_entity and tts:
        tips.append(f"Spraakdienst: {next((t for t in tts if 'nl' in t), tts[0])}")
    people = [e for e in entities if e.domain == "person"]
    phones = [n for n in index.services.get("notify", {}) if n.startswith("mobile_app_")]
    if not config.person_notify and len(people) > 1 and phones:
        tips.append("Telefoon per persoon (Meldingen): de deurbel gaat dan alleen naar wie thuis is")
    private = [c.entity_id for c in entities if c.domain == "camera" and index.guess_profile(c) == "off"
               and not any(p.startswith(f"{c.entity_id}=") for p in config.camera_profiles)]
    if private:
        tips.append(f"Camera's die automatisch genegeerd worden (controleer bij Camera's): {_names(private)}")
    fan = house_fan(index)
    if fan is not None and not config.ventilation_entity:
        tips.append(f"Centrale ventilatie: {fan.entity_id} (nu laat de agent ventileren aan je automatiseringen)")
    helper = mode_helper(index, config)
    if helper is not None and not config.mode_entity:
        tips.append(f"Huismodus wordt gelezen uit {helper.entity_id}")
    return tips


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
