import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.agent.loop import Agent
from app.tools.actions import resolve_confirmation
from app.tools.home import build_registry
from app.watcher.routines import accept_suggestion, analyze_routines, automation_for, find_routines

from .conftest import FakeLLM

registry = build_registry()
TZ = ZoneInfo("Europe/Amsterdam")


async def run(ctx, name, **args):
    return json.loads(await registry.run(ctx, name, args))


def logbook_entry(entity_id, state, day, hour, minute, **context):
    when = datetime(2026, 10, 1, hour, minute, tzinfo=TZ) + timedelta(days=day)
    return {"entity_id": entity_id, "state": state, "when": when.timestamp(), **context}


def test_find_routines_weekdays():
    entries = []
    for day in range(14):
        weekday = (datetime(2026, 10, 1) + timedelta(days=day)).weekday()
        if weekday < 5:
            entries.append(logbook_entry("light.keuken", "on", day, 6, 50 + day % 3))
        entries.append(logbook_entry("light.keuken", "off", day, 8 + day % 5, 0))  # no pattern
        entries.append(logbook_entry("switch.koffiezetapparaat", "on", day, 7, 0,
                                     context_event_type="automation_triggered"))  # automated: ignored
    routines = find_routines(entries, TZ)
    assert len(routines) == 1
    r = routines[0]
    assert (r.entity_id, r.state, r.time, r.weekdays_only, r.days) == ("light.keuken", "on", "06:50", True, 10)


def test_find_routines_around_midnight():
    entries = [logbook_entry("light.keuken", "off", day, 23, 55) for day in range(0, 14, 2)]
    entries += [logbook_entry("light.keuken", "off", day, 0, 5) for day in range(1, 14, 2)]
    (routine,) = find_routines(entries, TZ)
    assert routine.time in ("23:55", "00:00", "00:05") and routine.days >= 7


async def test_failed_confirmation_can_be_retried(ctx):
    result = await run(ctx, "call_service", domain="lock", service="unlock", entity_ids=["lock.voordeur"])

    async def broken(*args, **kwargs):
        raise RuntimeError("HA niet bereikbaar")
    working = ctx.ha.call_service
    ctx.ha.call_service = broken
    out = await resolve_confirmation(ctx, result["confirmation_id"], approve=True)
    assert out["status"] == "error"
    ctx.ha.call_service = working
    out = await resolve_confirmation(ctx, result["confirmation_id"], approve=True)
    assert out["status"] == "ok"


def test_automation_for_suggestion():
    config = automation_for({"entity_id": "cover.garagedeur", "state": "closed", "time": "22:30",
                             "weekdays_only": False, "description": "x"}, "Garagedeur")
    assert config["triggers"] == [{"trigger": "time", "at": "22:30:00"}]
    assert config["actions"] == [{"action": "cover.close_cover", "target": {"entity_id": "cover.garagedeur"}}]
    assert config["conditions"] == []


async def test_analyze_and_accept_routine(ctx):
    ctx.ha.logbook_entries = [logbook_entry("light.keuken", "on", day, 18, 30) for day in range(7)]
    assert await analyze_routines(ctx) == 1
    assert await analyze_routines(ctx) == 0  # no duplicates
    suggestion, = ctx.db.suggestions()
    assert "Keukenlamp" in suggestion["description"]
    result = await accept_suggestion(ctx, suggestion["id"])
    assert result["status"] == "ok"
    saved, = ctx.ha.saved_automations.values()
    assert saved["actions"][0]["action"] == "light.turn_on"
    assert ctx.db.suggestions() == []


async def test_create_automation_needs_confirmation(ctx):
    result = await run(
        ctx, "create_automation", alias="Gang nachtlicht",
        triggers=[{"trigger": "state", "entity_id": "binary_sensor.raam_slaapkamer", "to": "on"}],
        actions=[{"action": "light.turn_on", "target": {"entity_id": "light.keuken"}, "data": {"brightness_pct": 10}}],
    )
    assert result["status"] == "needs_confirmation"
    assert ctx.ha.saved_automations == {}
    out = await resolve_confirmation(ctx, result["confirmation_id"], approve=True)
    assert out["status"] == "ok"
    saved, = ctx.ha.saved_automations.values()
    assert saved["alias"] == "Gang nachtlicht"


async def test_create_automation_validation(ctx):
    bad_entity = await run(ctx, "create_automation", alias="x",
                           triggers=[{"trigger": "state", "entity_id": "binary_sensor.bestaat_niet"}],
                           actions=[{"action": "light.turn_on"}])
    assert "binary_sensor.bestaat_niet" in bad_entity["error"]
    ctx.ha.validation = {"triggers": {"valid": False, "error": "Invalid trigger"}, "actions": {"valid": True}}
    invalid = await run(ctx, "create_automation", alias="x", triggers=[{"trigger": "foo"}],
                        actions=[{"action": "light.turn_on"}])
    assert invalid["details"] == {"triggers": "Invalid trigger"}


async def test_dry_run_automation(ctx):
    ctx.config.dry_run = True
    result = await run(ctx, "create_automation", alias="x", triggers=[{"trigger": "time", "at": "07:00:00"}],
                       actions=[{"action": "light.turn_on", "target": {"entity_id": "light.keuken"}}])
    out = await resolve_confirmation(ctx, result["confirmation_id"], approve=True)
    assert out["status"] == "dry_run"
    assert ctx.ha.saved_automations == {}


async def test_memory_in_system_prompt(ctx):
    assert (await run(ctx, "remember", fact="Vincent wil 19,5 graden in de woonkamer"))["status"] == "ok"
    agent = Agent(ctx, registry)
    prompt = agent.system_prompt()
    assert "Vincent wil 19,5 graden" in prompt
    memory_id = ctx.db.memories()[0]["id"]
    await run(ctx, "forget", memory_id=memory_id)
    assert "19,5 graden" not in agent.system_prompt()


async def test_voice_yes_confirms_pending(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    ctx.llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [{"function": {
            "name": "call_service",
            "arguments": {"domain": "lock", "service": "unlock", "entity_ids": ["lock.voordeur"]}}}]},
        {"role": "assistant", "content": "Wil je dat ik de voordeur van het slot haal? Zeg ja om te bevestigen."},
    ])
    agent = Agent(ctx, registry)
    first = await agent.run("Doe de voordeur van het slot", "voice1", source="assist")
    assert len(first.pending) == 1
    # From Assist the confirmation is also pushed to the phone
    assert any(c[0] == "notify" for c in ctx.ha.calls)
    second = await agent.run("Ja", "voice1", source="assist")
    assert second.answer.startswith("Gedaan")
    assert ("lock", "unlock", {}, {"entity_id": ["lock.voordeur"]}) in ctx.ha.calls
    assert len(ctx.llm.requests) == 2  # "ja" handled without the LLM


async def test_recent_events_tool(ctx, notifier):
    await notifier.send("📷 Detectie bij voordeur", "Een bezorger met een pakket.", kind="camera")
    result = await run(ctx, "get_recent_events", hours=2)
    assert "bezorger" in result["events"][0]
