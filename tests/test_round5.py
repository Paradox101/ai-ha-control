"""Second improvement round on existing functionality (0.6.22)."""

from app.agent.loop import Agent
from app.tools.home import build_registry
from app.watcher.simulation import PresenceSimulator

from .test_security_comfort import act_everything


async def test_text_streamed_before_a_tool_call_is_kept_apart(ctx):
    responses = [
        {"role": "assistant", "content": "Even kijken.", "tool_calls": [
            {"function": {"name": "get_state", "arguments": {"entity_id": "light.keuken"}}}]},
        {"role": "assistant", "content": "De keukenlamp is aan."},
    ]

    async def chat(messages, tools=None, model=None, timeout=300, priority=1, on_delta=None, schema=None,
                   max_tokens=None):
        response = responses.pop(0)
        if on_delta is not None:
            await on_delta(response["content"])
        return response

    ctx.llm.chat = chat
    pieces = []

    async def on_delta(piece):
        pieces.append(piece)

    result = await Agent(ctx, build_registry()).run("bedankt", "c1", source="assist", on_delta=on_delta)
    assert result.answer == "De keukenlamp is aan."
    assert "".join(pieces) == "Even kijken.\n\nDe keukenlamp is aan."


async def test_simulation_remembers_its_lights_after_a_restart(ctx, notifier):
    await act_everything(ctx)
    ctx.db.audit("service", {"domain": "light", "service": "turn_on", "entity_ids": ["light.keuken"]},
                 "executed", "ok", "simulation")
    ctx.db.audit("service", {"domain": "light", "service": "turn_on", "entity_ids": ["light.woonkamer_plafond"]},
                 "executed", "ok", "simulation")
    ctx.db.audit("service", {"domain": "light", "service": "turn_off", "entity_ids": ["light.woonkamer_plafond"]},
                 "executed", "ok", "simulation")
    simulation = PresenceSimulator(ctx)  # fresh after a restart
    await simulation.tick("weg", None)  # vacation turned into "away": switch our own lights off
    assert ("light", "turn_off", {}, {"entity_id": ["light.keuken"]}) in ctx.ha.calls
    assert ("light", "turn_off", {}, {"entity_id": ["light.woonkamer_plafond"]}) not in ctx.ha.calls


# --- confirmations, cameras ------------------------------------------------------------------

async def test_ok_after_another_question_does_not_unlock_the_door(ctx):
    from app.tools.actions import perform

    ctx.source, ctx.conversation_id = "web", "c1"
    await perform(ctx, "lock", "unlock", ["lock.voordeur"])
    assert ctx.confirmations.list("c1")
    agent = Agent(ctx, build_registry())
    ctx.db.add_message("c1", "user", "Hoe warm is het?")  # a later, unrelated turn
    ctx.db.add_message("c1", "assistant", "21 graden.")
    for word in ("prima", "ja"):
        await agent.run(word, "c1", source="web")
    assert not [c for c in ctx.ha.calls if c[0] == "lock"]


async def test_yes_right_after_the_question_still_confirms(ctx):
    from app.tools.actions import perform

    ctx.source, ctx.conversation_id = "web", "c1"
    ctx.db.add_message("c1", "user", "Doe de voordeur van het slot")
    await perform(ctx, "lock", "unlock", ["lock.voordeur"])
    result = await Agent(ctx, build_registry()).run("ja", "c1", source="web")
    assert ("lock", "unlock") in [c[:2] for c in ctx.ha.calls] and "Gedaan" in result.answer


def baby_camera(ctx):
    ctx.index.update_state({"entity_id": "camera.babyfoon", "state": "idle",
                            "attributes": {"friendly_name": "Camera Babykamer"}})
    ctx.index.update_state({"entity_id": "binary_sensor.deur_babykamer", "state": "off",
                            "attributes": {"friendly_name": "Deur babykamer", "device_class": "door"}})
    for eid in ("camera.babyfoon", "binary_sensor.deur_babykamer"):
        ent = ctx.index.get(eid)
        ent.area = "Ouderslaapkamer"


async def test_private_cameras_stay_private(ctx):
    from app.tools.home import analyze_camera

    baby_camera(ctx)
    cam = ctx.index.get("camera.babyfoon")
    assert ctx.index.camera_profile(cam, []) == "off"
    door = ctx.index.get("binary_sensor.deur_babykamer")
    assert ctx.index.camera_for(door) is not None and ctx.index.camera_for(door, []) is None
    ctx.source = "autonomous"
    result = await analyze_camera(ctx, area="Ouderslaapkamer")
    assert "error" in result and not ctx.llm.requests
    ctx.source = "web"
    result = await analyze_camera(ctx, entity_id="camera.babyfoon")  # asked for by name: allowed
    assert result["images"] == ["camera.babyfoon"]


# --- history -------------------------------------------------------------------------------

async def test_meter_dip_is_not_a_reset(ctx):
    from app.tools.history import get_history

    ctx.ha.timelines = {}

    async def history(entity_ids, start_time, end_time=None):
        return {"sensor.stroom_totaal": [{"s": "1203.5", "lu": 1.0}, {"s": "1203.4", "lu": 2.0},
                                         {"s": "1203.6", "lu": 3.0}, {"s": "2.0", "lu": 4.0}]}

    ctx.ha.history = history
    result = await get_history(ctx, "sensor.stroom_totaal", hours=24)
    assert result["increase"] == 2.1  # 0.1 before the real reset, 2.0 after it


def test_clock_times_reach_back_to_last_night(ctx, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.tools import history

    now = datetime(2026, 10, 19, 8, 0, tzinfo=ZoneInfo("Europe/Amsterdam"))
    monkeypatch.setattr(history, "local_now", lambda tz: now.astimezone(tz))
    begin, end, _ = history.resolve_period(ctx, start="23:00")
    assert (begin.day, begin.hour, end) == (18, 23, now)
    begin, end, _ = history.resolve_period(ctx, start="23:00", end="06:00")
    assert (begin.day, begin.hour, end.day, end.hour) == (18, 23, 19, 6)
