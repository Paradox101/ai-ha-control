"""Evaluation: 'how often / when / how long' questions get exact numbers and times from the tools."""

import json

import pytest

from app.agent.loop import Agent
from app.tools import history
from app.tools.history import Episode, build_timeline, episodes, merge, resolve_period
from app.tools.home import build_autonomous_registry, build_registry
from tests.conftest import FakeLLM
from tests.history_day import EVAL_CASES, NOW, TZ, at, load_day


@pytest.fixture
def day(ctx, monkeypatch):
    monkeypatch.setattr(history, "local_now", lambda tz: NOW.astimezone(tz))
    load_day(ctx.index, ctx.ha)
    return ctx


async def run(ctx, name, **arguments):
    return json.loads(await build_registry().run(ctx, name, arguments))


@pytest.mark.parametrize("case", EVAL_CASES, ids=[c.question for c in EVAL_CASES])
async def test_eval_question(day, case):
    result = await run(day, case.tool, **case.arguments)
    assert "error" not in result, result
    for key, value in case.expect.items():
        assert result[key] == value, (key, result)
    text = json.dumps(result, ensure_ascii=False)
    for snippet in case.contains:
        assert snippet in text, (snippet, result)


async def test_front_door_counts_each_visit_once(day):
    """Duplicate Duidelijk/Vloeiend sensors, flicker and an unavailable blip do not add visits."""
    result = await run(day, "get_detections", what="persoon", where="voordeur")
    place = result["places"][0]
    assert place["place"] == "Voordeur" and place["count"] == 3
    assert (place["first"], place["last"]) == ("08:12", "14:05")
    assert place["sensors"] == {"binary_sensor.voordeur_person": 4, "binary_sensor.voordeur_duidelijk_persoon": 3,
                                "binary_sensor.voordeur_vloeiend_persoon": 2}
    assert len(place["times"]) == 3


async def test_where_can_be_the_camera_entity(day):
    by_camera = await run(day, "get_detections", what="person", where="camera.voordeur_fluent")
    assert by_camera["total"] == 3


async def test_camera_state_is_not_a_detection(day):
    """The bug from the screenshot: the camera's 'idle' history was read as 'one person seen'."""
    result = await run(day, "get_history", entity_id="camera.voordeur_clear", today=True)
    assert result["number_of_changes"] == 0 and result["changes"] == []
    assert "get_detections" in result["hint"]


async def test_initial_state_is_not_a_change(day):
    result = await run(day, "get_history", entity_id="binary_sensor.voordeur_visitor", period="yesterday")
    assert result["number_of_changes"] == 0 and result["count"] == 0
    assert "Geen enkele wijziging" in result["note"]


async def test_already_open_at_midnight_is_reported_not_counted(day):
    result = await run(day, "get_history", entity_id="binary_sensor.raam_slaapkamer", today=True)
    assert result["state_at_start"] == "open" and result["state_at_end"] == "open"
    assert result["periods"][0].startswith("al actief")
    assert result["periods"][-1].startswith("15:00 (nog bezig")


async def test_unknown_kind_or_place(day):
    missing = await run(day, "get_detections", what="pakketje", where="voordeur")
    assert "error" in missing and "persoon" in missing["available_kinds"]
    nowhere = await run(day, "get_detections", where="zolder")
    assert "error" in nowhere and "Voordeur" in nowhere["places"]
    bad = await run(day, "get_detections", period="ooit")
    assert "periode" in bad["error"]


async def test_ai_descriptions_are_added(day):
    event = day.db.add_event("camera", "Detectie bij Voordeur", "Een postbode met een pakket.", "camera.voordeur_clear")
    day.db._conn.execute("UPDATE events SET ts = ? WHERE id = ?", (at("15:10").timestamp(), event))
    result = await run(day, "get_detections", what="person", where="voordeur", hours=1)
    assert "postbode" in result["places"][0]["ai_descriptions"][0]


def test_periods(ctx, monkeypatch):
    monkeypatch.setattr(history, "local_now", lambda tz: NOW.astimezone(tz))
    begin, end, label = resolve_period(ctx, "gisteren")
    assert (begin, end, label) == (at("00:00", NOW.replace(day=18)), at("00:00"), "18-10 (hele dag)")
    begin, end, _ = resolve_period(ctx, "zaterdag")
    assert begin.day == 17 and end.day == 18
    begin, end, _ = resolve_period(ctx, "vannacht")
    assert (begin.day, begin.hour, end.day, end.hour) == (18, 22, 19, 7)
    begin, end, _ = resolve_period(ctx, start="2026-10-18", end="2026-10-18")
    assert (begin.day, end.day) == (18, 19)
    begin, end, _ = resolve_period(ctx, start="09:00")
    assert (begin.hour, end) == (9, NOW)
    begin, _, _ = resolve_period(ctx, "this_week")
    assert begin == at("00:00")  # it is Monday
    with pytest.raises(ValueError):
        resolve_period(ctx, start="2026-10-20")  # in the future


def test_merge_and_resume():
    t = lambda clock: at(clock).astimezone(TZ)  # noqa: E731
    timeline = build_timeline([{"s": "on", "lu": t("10:00").timestamp()},
                               {"s": "unavailable", "lu": t("10:05").timestamp()},
                               {"s": "on", "lu": t("10:20").timestamp()},
                               {"s": "off", "lu": t("10:30").timestamp()}], t("09:00"), t("11:00"))
    found = episodes(timeline, {"on"}, t("09:00"))
    assert [e.resumed for e in found] == [False, True]
    assert len(merge(found, 0)) == 1  # back from unavailable: the same episode
    apart = [Episode(t("10:00"), t("10:01")), Episode(t("10:01:30"), t("10:02")), Episode(t("10:10"), t("10:11"))]
    assert len(merge(apart, 60)) == 2


def test_tools_are_offered():
    assert "get_detections" in build_registry().names
    assert "get_detections" in build_autonomous_registry().names


async def test_prompt_routes_detection_questions(day):
    prompt = Agent(day, build_registry()).system_prompt()
    assert "get_detections" in prompt and "Tel nooit zelf" in prompt
    assert "maandag 19-10-2026 15:30" in prompt  # the same clock as the tools


async def test_agent_answers_from_the_count(day):
    """End to end: the model's tool call reaches Home Assistant's history and the count comes back to it."""
    day.llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [{"function": {
            "name": "get_detections", "arguments": {"what": "persoon", "where": "voordeur"}}}]},
        {"role": "assistant", "content": "Vandaag 3 keer: om 08:12, 12:47 en 14:05."},
    ])
    result = await Agent(day, build_registry()).run(EVAL_CASES[0].question, "eval")
    tool_result = json.loads(day.llm.requests[1]["messages"][-1]["content"])
    assert tool_result["total"] == 3
    assert result.answer.startswith("Vandaag 3 keer")
