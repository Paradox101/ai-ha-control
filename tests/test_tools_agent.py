import json

from app.agent.loop import Agent
from app.tools.actions import execute
from app.tools.home import build_registry

from .conftest import FakeLLM

registry = build_registry()


async def run(ctx, name, **args):
    return json.loads(await registry.run(ctx, name, args))


async def test_control_light_executes(ctx):
    result = await run(ctx, "control_light", entity_ids="light.woonkamer_plafond", action="on", brightness_pct=40)
    assert result["status"] == "ok"
    assert ctx.ha.calls == [("light", "turn_on", {"brightness_pct": 40}, {"entity_id": ["light.woonkamer_plafond"]})]
    assert ctx.db.audit_log()[0]["decision"] == "executed"


async def test_unknown_entity_suggests(ctx):
    result = await run(ctx, "control_light", entity_ids=["light.plafond"], action="on")
    assert result["status"] == "error"
    assert "light.woonkamer_plafond" in result["error"]
    assert ctx.ha.calls == []


async def test_dry_run(ctx):
    ctx.config.dry_run = True
    result = await run(ctx, "turn_on_off", entity_ids=["switch.koffiezetapparaat"], action="on")
    assert result["switch"]["status"] == "dry_run"
    assert ctx.ha.calls == []


async def test_lock_needs_confirmation(ctx):
    result = await run(ctx, "call_service", domain="lock", service="unlock", entity_ids=["lock.voordeur"])
    assert result["status"] == "needs_confirmation"
    assert ctx.ha.calls == []
    pending = ctx.confirmations.pop(result["confirmation_id"])
    out = await execute(ctx, pending.domain, pending.service, pending.entity_ids, pending.data, confirmed=True)
    assert out["status"] == "ok"
    assert ctx.ha.calls[0][:2] == ("lock", "unlock")


async def test_denied_service(ctx):
    result = await run(ctx, "call_service", domain="shell_command", service="run")
    assert result["status"] == "denied"


async def test_climate_validation(ctx):
    assert "error" in await run(ctx, "set_climate", entity_id="climate.thermostaat", temperature=45)
    assert "error" in await run(ctx, "set_climate", entity_id="climate.thermostaat", hvac_mode="cool")
    result = await run(ctx, "set_climate", entity_id="climate.thermostaat", temperature=21.5)
    assert result["temperature"]["status"] == "ok"


async def test_history_numeric(ctx):
    result = await run(ctx, "get_history", entity_id="sensor.woonkamer_temperatuur", hours=12)
    assert (result["min"], result["max"], result["last"]) == (20.0, 22.0, 21.0)


async def test_camera(ctx):
    result = await run(ctx, "analyze_camera", entity_id="camera.voordeur_fluent", question="Pakketje?")
    assert "pakketje" in result["description"]


async def test_agent_loop(ctx):
    ctx.llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search_entities", "arguments": {"query": "lamp", "area": "woonkamer"}}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "control_light",
                          "arguments": {"entity_ids": ["light.woonkamer_plafond"], "action": "on"}}}]},
        {"role": "assistant", "content": "<think>ok</think>De plafondlamp in de woonkamer is aan."},
    ])
    agent = Agent(ctx, registry)
    result = await agent.run("Doe de lamp in de woonkamer aan", "conv1")
    assert result.answer == "De plafondlamp in de woonkamer is aan."
    assert [a["tool"] for a in result.actions] == ["search_entities", "control_light"]
    assert ctx.ha.calls[0][1] == "turn_on"
    # Second request contains the tool result of the search
    tool_msgs = [m for m in ctx.llm.requests[1]["messages"] if m["role"] == "tool"]
    assert "light.woonkamer_plafond" in tool_msgs[0]["content"]
    # History is persisted for the next turn
    assert [m["role"] for m in ctx.db.recent_messages("conv1")] == ["user", "assistant"]


async def test_agent_text_tool_call_fallback_and_budget(ctx):
    ctx.config.max_tool_calls = 1
    ctx.llm = FakeLLM([
        {"role": "assistant", "content": '{"name": "get_home_overview", "arguments": {}}'},
        {"role": "assistant", "content": "Alles in orde."},
    ])
    result = await Agent(ctx, registry).run("Hoe staat het huis ervoor?", "conv2")
    assert [a["tool"] for a in result.actions] == ["get_home_overview"]
    assert ctx.llm.requests[1]["tools"] is None
    assert result.answer == "Alles in orde."
