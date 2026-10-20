import asyncio
import io
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.agent.autonomous import AutonomousAgent
from app.agent.loop import CHECK_FIRST, Agent, needs_check
from app.autonomy import Autonomy, restore_calls, undo_action
from app.ha.status import StatusPublisher, ensure_kill_switch
from app.llm.ollama import LLMError, PriorityLock
from app.storage.db import Database
from app.tools.home import build_autonomous_registry, build_registry
from app.watcher.baseline import MIN_SAMPLES, Baseline, house_mode
from app.watcher.incidents import IncidentManager, SnapshotStore
from app.watcher.patrol import Patrol, difference, signature
from app.watcher.rules import Watcher, apply_feedback
from app.watcher.vision import PASSERBY, SUSPICIOUS, UNKNOWN, VISITOR, parse

from .conftest import FakeLLM


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def set_state(index, entity_id, state, minutes_ago=0, **attrs):
    ent = index.get(entity_id)
    index.update_state({"entity_id": entity_id, "state": state, "attributes": {**ent.attributes, **attrs},
                        "last_changed": ago(minutes_ago)})


def pushes(ctx):
    return [c for c in ctx.ha.calls if c[0] in ("notify", "persistent_notification")]


@pytest.fixture
def auto(ctx, notifier):
    """Context wired like the app: autonomy, incidents, kill switch on."""
    ctx.index.update_state({"entity_id": "input_boolean.ai_autonoom", "state": "on", "attributes": {}})
    ctx.autonomy = Autonomy(ctx.config, ctx.index, ctx.db)
    ctx.incidents = IncidentManager(ctx.db, notifier)
    ctx.source = "autonomous"
    ctx.reason = "test"
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    return ctx


# --- chat quality ------------------------------------------------------------------

async def test_answer_without_checking_is_sent_back(ctx):
    ctx.llm = FakeLLM([
        {"role": "assistant", "content": "De robotstofzuiger heeft vandaag niet gedraaid."},  # from memory
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_history", "arguments": {"entity_id": "switch.koffiezetapparaat", "today": True}}}]},
        {"role": "assistant", "content": "Volgens de geschiedenis is hij vandaag niet aangezet."},
    ])
    result = await Agent(ctx, build_registry()).run("Is het koffiezetapparaat vandaag aan geweest?", "q1")
    assert [a["tool"] for a in result.actions] == ["get_history"]
    assert ctx.llm.requests[1]["messages"][-1]["content"] == CHECK_FIRST
    assert "geschiedenis" in result.answer


async def test_check_remark_is_not_shown_to_the_user(ctx):
    ctx.llm = FakeLLM([
        {"role": "assistant", "content": "Het was vannacht 19,9 °C."},
        {"role": "assistant", "content": "De momentopname beantwoordt de vraag **volledig**. Het was vannacht 19,9 °C."},
    ])
    result = await Agent(ctx, build_registry()).run("Hoe warm was het vannacht in de slaapkamer?", "q3")
    assert result.answer == "Het was vannacht 19,9 °C."


async def test_small_talk_is_not_sent_back(ctx):
    ctx.llm = FakeLLM([{"role": "assistant", "content": "Graag gedaan!"}])
    result = await Agent(ctx, build_registry()).run("bedankt", "q2")
    assert result.answer == "Graag gedaan!" and len(ctx.llm.requests) == 1
    assert needs_check("Jawel?") and needs_check("doe de lamp aan") and not needs_check("top")


async def test_system_prompt_has_live_snapshot(ctx):
    ctx.index.update_state({"entity_id": "alarm_control_panel.huis", "state": "armed_away",
                            "attributes": {"friendly_name": "Alarm"}})
    prompt = Agent(ctx, build_registry()).system_prompt()
    assert "Lampen aan (1): Keuken: Keukenlamp" in prompt
    assert "Deuren/ramen (1 sensoren): OPEN: Slaapkamer: Raam slaapkamer" in prompt and "Huismodus: " in prompt
    assert "Alarm: Alarm = ingeschakeld (afwezig)" in prompt and "Slot: Voordeur slot = op slot" in prompt
    set_state(ctx.index, "binary_sensor.raam_slaapkamer", "off")
    assert "Deuren/ramen (1 sensoren): alles dicht" in Agent(ctx, build_registry()).system_prompt()


async def test_history_today(ctx):
    result = json.loads(await build_registry().run(ctx, "get_history",
                                                   {"entity_id": "sensor.woonkamer_temperatuur", "today": True}))
    assert 0.25 <= result["period_hours"] <= 24


# --- autonomy ---------------------------------------------------------------------

def test_autonomy_levels(auto):
    a = auto.autonomy
    assert a.decide("light", "turn_on", ["light.keuken"], {}) == ("act", "")
    assert a.decide("lock", "unlock", ["lock.voordeur"], {})[0] == "ask"
    assert a.decide("light", "turn_on", ["light.keuken", "lock.voordeur"], {})[0] == "ask"  # strictest wins
    assert a.decide("climate", "set_temperature", ["climate.thermostaat"], {"temperature": 25})[0] == "ask"
    assert a.decide("climate", "set_temperature", ["climate.thermostaat"], {"temperature": 21.5})[0] == "act"
    assert a.decide("light", "flash", ["light.keuken"], {})[0] == "ask"  # not reversible
    auto.config.autonomy = ["light=observe"]
    assert a.decide("light", "turn_on", ["light.keuken"], {})[0] == "observe"


def test_kill_switch_and_rate_limit(auto):
    a = auto.autonomy
    set_state(auto.index, "input_boolean.ai_autonoom", "off")
    level, reason = a.decide("light", "turn_on", ["light.keuken"], {})
    assert level == "ask" and "noodknop" in reason
    set_state(auto.index, "input_boolean.ai_autonoom", "on")
    auto.config.max_autonomous_actions_per_hour = 1
    auto.db.add_action("light", "turn_on", ["light.keuken"], {}, {})
    assert "maximum" in a.decide("light", "turn_on", ["light.keuken"], {})[1]


async def test_autonomous_action_is_recorded_notified_and_undoable(auto):
    result = json.loads(await build_autonomous_registry().run(
        auto, "control_light", {"entity_ids": ["light.woonkamer_plafond"], "action": "on"}))
    assert result["status"] == "ok"
    (_, _, data, _), = pushes(auto)
    assert data["title"] == "🤖 Zelf gedaan"
    undo = data["data"]["actions"][0]["action"]
    assert undo == f"AIHA_UNDO_{result['action_id']}"
    out = await undo_action(auto, result["action_id"])
    assert out["status"] == "ok"
    assert auto.ha.calls[-1][:2] == ("light", "turn_off")  # it was off before
    assert (await undo_action(auto, result["action_id"]))["status"] == "error"  # only once


async def test_autonomous_lock_asks_and_kill_switch_is_untouchable(auto):
    registry = build_autonomous_registry()
    result = json.loads(await registry.run(auto, "call_service",
                                           {"domain": "lock", "service": "unlock", "entity_ids": ["lock.voordeur"]}))
    assert result["status"] == "needs_confirmation"
    assert not any(c[:2] == ("lock", "unlock") for c in auto.ha.calls)
    result = json.loads(await registry.run(auto, "turn_on_off",
                                           {"entity_ids": ["input_boolean.ai_autonoom"], "action": "off"}))
    assert result["input_boolean"]["status"] == "denied"


def test_restore_calls():
    assert restore_calls("light.x", {"state": "on", "attributes": {"brightness": 80}}) == \
        [("light", "turn_on", {"brightness": 80})]
    assert restore_calls("climate.x", {"state": "heat", "attributes": {"temperature": 19, "hvac_modes": ["heat"]}}) == \
        [("climate", "set_hvac_mode", {"hvac_mode": "heat"}), ("climate", "set_temperature", {"temperature": 19})]
    assert restore_calls("switch.x", {"state": "off"}) == [("switch", "turn_off", {})]


# --- GPU queue --------------------------------------------------------------------------

async def test_priority_lock_serves_safety_first():
    lock, order = PriorityLock(), []
    await lock.acquire(2)

    async def job(priority, name):
        await lock.acquire(priority)
        order.append(name)
        lock.release()

    tasks = [asyncio.create_task(job(3, "patrol")), asyncio.create_task(job(1, "chat")),
             asyncio.create_task(job(0, "safety"))]
    await asyncio.sleep(0)
    lock.release()
    await asyncio.gather(*tasks)
    assert order == ["safety", "chat", "patrol"]


# --- knowing what is normal ------------------------------------------------------------

def test_house_mode(ctx):
    now = datetime(2026, 10, 19, 14, 0)
    assert house_mode(ctx.index, ctx.config, now) == "thuis"
    assert house_mode(ctx.index, ctx.config, datetime(2026, 10, 19, 23, 30)) == "slapen"
    set_state(ctx.index, "person.vincent", "not_home", 30)
    assert house_mode(ctx.index, ctx.config, now) == "weg"
    set_state(ctx.index, "person.vincent", "not_home", 60 * 30)
    assert house_mode(ctx.index, ctx.config, now) == "vakantie"


def test_house_mode_from_mode_scenes(ctx):
    now = datetime(2026, 10, 19, 14, 0)
    for eid, name, minutes in (("scene.afwezig", "Afwezig", 300), ("scene.gast", "Gast", 60 * 24 * 150),
                               ("scene.slapen", "Slapen", 960), ("scene.thuis", "Thuis", 360),
                               ("scene.film_kijken", "Film kijken", 5)):
        ctx.index.update_state({"entity_id": eid, "state": ago(minutes), "attributes": {"friendly_name": name}})
    assert house_mode(ctx.index, ctx.config, now) == "thuis"  # Vincent is home: presence beats the away scene
    ctx.index.update_state({"entity_id": "person.vincent", "state": "not_home", "last_changed": ago(300),
                            "attributes": {"friendly_name": "Vincent"}})
    assert house_mode(ctx.index, ctx.config, now) == "weg"  # Afwezig was activated last (5 hours ago)
    ctx.index.update_state({"entity_id": "scene.gast", "state": ago(1), "attributes": {"friendly_name": "Gast"}})
    assert house_mode(ctx.index, ctx.config, now) == "gast"
    ctx.index.update_state({"entity_id": "scene.afwezig", "state": ago(0.5), "attributes": {"friendly_name": "Afwezig"}})
    ctx.index.update_state({"entity_id": "scene.afwezig", "state": ago(60 * 30), "attributes": {"friendly_name": "Afwezig"}})
    ctx.index.update_state({"entity_id": "scene.gast", "state": ago(60 * 40), "attributes": {"friendly_name": "Gast"}})
    ctx.index.update_state({"entity_id": "scene.thuis", "state": ago(60 * 50), "attributes": {"friendly_name": "Thuis"}})
    ctx.index.update_state({"entity_id": "scene.slapen", "state": ago(60 * 60), "attributes": {"friendly_name": "Slapen"}})
    assert house_mode(ctx.index, ctx.config, now) == "vakantie"  # away for more than a day


def test_sleep_scene_lasts_one_night(ctx):
    now = datetime(2026, 10, 19, 14, 0)
    ctx.index.update_state({"entity_id": "scene.slapen", "state": ago(60), "attributes": {"friendly_name": "Slapen"}})
    assert house_mode(ctx.index, ctx.config, now) == "slapen"
    ctx.index.update_state({"entity_id": "scene.slapen", "state": ago(60 * 13),
                            "attributes": {"friendly_name": "Slapen"}})
    assert house_mode(ctx.index, ctx.config, now) == "thuis"


def test_baseline_learns_and_flags_outliers(ctx):
    baseline = Baseline(ctx.db, ctx.index)
    now = datetime(2026, 10, 19, 14, 0)
    for i in range(MIN_SAMPLES):
        set_state(ctx.index, "sensor.woonkamer_temperatuur", str(20 + (i % 3) * 0.2))
        baseline._last_sample.clear()
        assert baseline.sample(now) == []
    set_state(ctx.index, "sensor.woonkamer_temperatuur", "29.5")
    baseline._last_sample.clear()
    (anomaly,) = baseline.sample(now)
    assert "ongewoon hoog" in anomaly.text and anomaly.key == "anomaly:sensor.woonkamer_temperatuur:hoog"
    ctx.db.mute(anomaly.key)  # "dit is normaal"
    baseline._last_sample.clear()
    assert baseline.sample(now) == []


def test_activity_at_unusual_hour_after_learning(ctx):
    baseline = Baseline(ctx.db, ctx.index)
    ent = ctx.index.get("binary_sensor.raam_slaapkamer")
    assert baseline.activity(ent, datetime(2026, 10, 19, 3, 0)) is None  # still learning
    ctx.db._conn.execute("UPDATE observed SET first_seen = ?", (time.time() - 30 * 86400,))
    assert baseline.activity(ent, datetime(2026, 10, 19, 4, 0)) is not None  # never active at 4:00
    assert baseline.activity(ent, datetime(2026, 10, 19, 3, 0)) is None  # 3:00 has been seen before


def test_offline_only_after_seen_available(ctx):
    baseline = Baseline(ctx.db, ctx.index)
    ctx.index.update_state({"entity_id": "sensor.dood", "state": "unavailable", "attributes": {},
                            "last_changed": ago(600)})
    assert baseline.offline() == []  # was never available: ignored
    set_state(ctx.index, "sensor.woonkamer_temperatuur", "20")
    baseline.offline()
    set_state(ctx.index, "sensor.woonkamer_temperatuur", "unavailable", 90)
    (anomaly,) = baseline.offline()
    assert "onbereikbaar" in anomaly.text
    assert baseline.offline() == []  # once per outage


# --- incidents -----------------------------------------------------------------------------

async def test_incident_correlation_and_escalation(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    first = await incidents.report("presence", "Detectie bij voordeur", "Persoon bij de poort", severity="info")
    second = await incidents.report("presence", "Detectie bij achtertuin", "Persoon in de achtertuin", severity="info")
    assert first == second and len(pushes(ctx)) == 1  # one story, one notification
    await incidents.report("presence", "x", "Persoon probeert de achterdeur", severity="warning")
    assert len(pushes(ctx)) == 2  # escalation is pushed
    assert len(ctx.db.get_incident(first)["events"]) == 3
    ctx.db._conn.execute("UPDATE incidents SET updated = ?", (time.time() - 2 * 86400,))
    assert incidents.resolve_stale() == 1


async def test_feedback_down_on_anomaly_mutes_it(ctx, notifier):
    event_id = await notifier.send("Ongebruikelijk", "Vriezer warm", kind="anomaly", ref="anomaly:sensor.vriezer:hoog")
    apply_feedback(ctx.db, event_id, -1)
    assert ctx.db.is_muted("anomaly:sensor.vriezer:hoog")
    assert ctx.db.negative_feedback()[0]["title"] == "Ongebruikelijk"


# --- autonomous agent ----------------------------------------------------------------------

async def test_agent_skips_llm_when_nothing_happened(auto):
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    assert await agent.run() is None
    assert auto.llm.requests == []


async def test_agent_opens_incident_and_plans_followup(auto):
    auto.llm = FakeLLM([
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "open_incident", "arguments": {
                "title": "Vriezer warmt op", "summary": "De vriezer is -8 graden", "severity": "warning"}}},
            {"function": {"name": "schedule_followup", "arguments": {"minutes": 15, "note": "Vriezer opnieuw checken"}}},
        ]},
        {"role": "assistant", "content": "Incident geopend en controle gepland."},
    ])
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    agent.observe("Afwijking: Vriezer is ongewoon hoog: -8 °C", urgent=True)
    summary = await agent.run("test")
    assert summary == "Incident geopend en controle gepland."
    report = auto.llm.requests[0]["messages"][1]["content"]
    assert "Vriezer is ongewoon hoog" in report and "Huismodus" in report
    assert auto.incidents.active()[0]["title"] == "Vriezer warmt op"
    assert len(pushes(auto)) == 1
    assert auto.db.pending_followups()[0]["note"] == "Vriezer opnieuw checken"
    assert auto.db.agent_runs()[0]["tools"] == ["open_incident", "schedule_followup"]


async def test_agent_falls_back_when_llm_is_down(auto):
    class DownLLM(FakeLLM):
        async def chat(self, *args, **kwargs):
            raise LLMError("Ollama weg")
    auto.llm = DownLLM()
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    agent.observe("Afwijking: deur open om 03:00", urgent=True)
    await agent.run()
    assert "AI niet bereikbaar" in auto.incidents.active()[0]["title"]


async def test_watcher_feeds_agent_and_mode_change(auto, notifier):
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    watcher = Watcher(auto, notifier)
    watcher.agent = agent
    set_state(auto.index, "lock.voordeur", "unlocked")
    await watcher.handle_state_changed({"entity_id": "lock.voordeur", "old_state": {"state": "locked"},
                                        "new_state": {"entity_id": "lock.voordeur", "state": "unlocked"}})
    assert "Voordeur slot: locked → unlocked" in agent.situation_report([])
    assert agent.set_mode("weg") and not agent.set_mode("weg")
    assert agent._needs_thinking([])


# --- camera patrol -------------------------------------------------------------------------

def jpeg(color, box=None):
    img = Image.new("RGB", (320, 180), color)
    if box:
        img.paste((255, 255, 255), box)
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


def test_signature_ignores_brightness_but_sees_objects():
    base = signature(jpeg((60, 60, 60)))
    assert difference(base, signature(jpeg((120, 120, 120)))) < 5     # dusk/clouds
    assert difference(base, signature(jpeg((60, 60, 60), (100, 40, 220, 160)))) > 12  # something appeared


async def test_patrol_reports_only_changed_and_relevant(auto, tmp_path):
    images = iter([jpeg((60, 60, 60)), jpeg((60, 60, 60), (100, 40, 220, 160))])
    async def snapshot(entity_id):
        return next(images)
    auto.ha.camera_snapshot = snapshot
    async def describe(image, question, priority=1, schema=None):
        return "VERDACHT: iemand met een capuchon bij de schuur"
    auto.llm.describe_image = describe
    auto.index.cameras = lambda: [auto.index.get("camera.voordeur_clear")]
    patrol = Patrol(auto, auto.incidents, SnapshotStore(str(tmp_path)))
    seen = []
    patrol.on_observation = seen.append
    assert await patrol.run() == 0   # first picture: nothing to compare with
    assert await patrol.run() == 1
    incident = auto.incidents.active()[0]
    assert incident["severity"] == "warning" and "capuchon" in incident["summary"]
    assert seen and list((tmp_path / "snapshots").glob("*.jpg"))


def test_vision_categories():
    assert parse("BEZOEK: een bezorger bij de deur") == (VISITOR, "een bezorger bij de deur")
    assert parse("**VOORBIJGANGER** - twee fietsers op straat")[0] == PASSERBY
    assert parse("VERDACHT. Iemand klimt over het hek")[0] == SUSPICIOUS
    assert parse("Er staat een pakketje.") == (UNKNOWN, "Er staat een pakketje.")
    # Newline after the category, and scenery that isn't needed in a notification
    assert parse("VOORBIJGANGER\nEen persoon rijdt een fiets voorbij. De omgeving toont huizen en auto's.") == (
        PASSERBY, "Een persoon rijdt een fiets voorbij.")


# --- HA entities, kill switch, migration ------------------------------------------------------

async def test_status_publisher_and_kill_switch(auto):
    publisher = StatusPublisher(auto.ha, auto.db, auto.autonomy)
    await auto.incidents.report("presence", "Detectie", "Persoon", severity="warning")
    await publisher.publish("weg", "Alles rustig")
    assert auto.ha.states_set["sensor.ai_huis_status"][0] == "aandacht"
    assert auto.ha.states_set["binary_sensor.ai_bezorgd"][0] == "on"
    assert auto.ha.states_set["sensor.ai_huismodus"][0] == "weg"
    await ensure_kill_switch(auto.ha, auto.index, "input_boolean.nieuwe_noodknop")
    assert auto.ha.created_booleans == ["nieuwe noodknop"]


def test_migration_adds_columns(tmp_path):
    conn = sqlite3.connect(tmp_path / "ai_ha_control.db")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, "
                 "title TEXT NOT NULL, message TEXT NOT NULL, entity_id TEXT, pushed INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    db = Database(str(tmp_path))
    event_id = db.add_event("anomaly", "t", "m", ref="anomaly:x", incident_id=1)
    assert db.get_event(event_id)["ref"] == "anomaly:x"


async def test_agent_knows_about_automations(auto):
    auto.index.update_state({"entity_id": "automation.vertrek", "state": "on",
                             "attributes": {"friendly_name": "Vertrek", "last_triggered": "2026-10-19T08:00:00+00:00"}})
    registry = build_autonomous_registry()
    assert {"list_automations", "get_automation", "related_automations"} <= set(registry.names)
    result = json.loads(await registry.run(auto, "related_automations", {"entity_id": "light.keuken"}))
    assert result["automations"] == ["automation.vertrek (Vertrek), laatst: 2026-10-19 10:00"]  # shown in local time
    agent = AutonomousAgent(auto, registry, auto.incidents)
    agent.note_automation({"name": "Vertrek", "entity_id": "automation.vertrek"})
    assert "Automatisering uitgevoerd: Vertrek" in agent.situation_report([])


async def test_away_heartbeat_skips_routine_changes(auto):
    """While away, our own actions and automations adjusting the heating do not wake the model."""
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    agent.set_mode("weg")
    agent._observations.clear()
    agent.note_automation({"name": "Nachtverlaging", "entity_id": "automation.nacht"})
    auto.index.update_state({"entity_id": "climate.woonkamer", "state": "heat", "attributes": {}})
    agent.note_state_change(auto.index.get("climate.woonkamer"), "off", "heat", origin="automatisering")
    assert not agent._needs_thinking([])
    assert await agent.run() is None
    assert "Nachtverlaging" in agent.situation_report([])  # still reported at the next real heartbeat
    set_state(auto.index, "lock.voordeur", "unlocked")
    agent.note_state_change(auto.index.get("lock.voordeur"), "locked", "unlocked", origin="automatisering")
    assert agent._needs_thinking([])


async def test_background_calls_cap_answer_length(auto):
    from app.llm.ollama import AGENT_MAX_TOKENS
    agent = AutonomousAgent(auto, build_autonomous_registry(), auto.incidents)
    agent.observe("Afwijking: test")
    await agent.run()
    assert auto.llm.requests[-1].get("max_tokens") == AGENT_MAX_TOKENS
