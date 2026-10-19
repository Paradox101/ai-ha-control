import asyncio
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.agent.autonomous import AutonomousAgent
from app.autonomy import Autonomy
from app.comfort import ComfortController, profile_suggestions, save_profile, set_comfort_profile
from app.ha.context import AGENT, AUTOMATION, DEVICE, RESIDENT, ContextTracker
from app.ha.index import EntityIndex
from app.ha.status import StatusPublisher
from app.main import access_middleware
from app.notify import Notifier
from app.security import is_security_entity, payload_touches_security
from app.storage.db import Database
from app.tools import actions
from app.tools.actions import ConfirmationStore, PendingAction, perform, resolve_confirmation, verify_action
from app.tools.home import build_autonomous_registry
from app.watcher.baseline import MIN_SAMPLES, Baseline
from app.watcher.incidents import IncidentManager
from app.watcher.rules import Watcher
from app.watcher.scheduler import Scheduler

from .conftest import FakeLLM


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def set_state(index, entity_id, state, minutes_ago=0, **attrs):
    ent = index.get(entity_id)
    base = ent.attributes if ent else {}
    index.update_state({"entity_id": entity_id, "state": state, "attributes": {**base, **attrs},
                        "last_changed": ago(minutes_ago)})


def event(index, entity_id, old, new, context=None, **attrs):
    set_state(index, entity_id, new, **attrs)
    ent = index.get(entity_id)
    return {"entity_id": entity_id, "old_state": {"state": old, "attributes": {}},
            "new_state": {"entity_id": entity_id, "state": new, "attributes": ent.attributes,
                          "context": context or {}}}


def pushes(ctx):
    return [c for c in ctx.ha.calls if c[0] in ("notify", "persistent_notification")]


def titles(ctx):
    return [c[2]["title"] for c in pushes(ctx)]


def service_calls(ctx):
    return [c for c in ctx.ha.calls if c[0] not in ("notify", "persistent_notification")]


async def act_everything(ctx):
    ctx.index.update_state({"entity_id": "input_boolean.ai_autonoom", "state": "on", "attributes": {}})
    ctx.autonomy = Autonomy(ctx.config, ctx.index, ctx.db)
    ctx.incidents = IncidentManager(ctx.db, ctx.notifier)
    ctx.source, ctx.reason = "autonomous", "test"
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    return ctx


# --- doors and the alarm, whatever the settings say ------------------------------------------

def test_security_entities_by_device_and_name():
    idx = EntityIndex()
    idx.load(
        [{"entity_id": "lock.nuki", "state": "locked", "attributes": {"friendly_name": "Nuki"}},
         {"entity_id": "switch.nuki_relais", "state": "off", "attributes": {"friendly_name": "Relais"}},
         {"entity_id": "switch.poort_opener", "state": "off", "attributes": {"friendly_name": "Poort"}},
         {"entity_id": "switch.outdoor_lights", "state": "off", "attributes": {"friendly_name": "Outdoor"}},
         {"entity_id": "switch.deurbel_gong", "state": "off", "attributes": {"friendly_name": "Deurbel gong"}},
         {"entity_id": "light.voordeur", "state": "off", "attributes": {"friendly_name": "Lamp voordeur"}},
         {"entity_id": "scene.alarm_aan", "state": "x", "attributes": {"friendly_name": "Alarm aan"}},
         {"entity_id": "cover.garage", "state": "closed",
          "attributes": {"friendly_name": "Kanteldeur", "device_class": "garage"}}],
        devices=[{"id": "d1", "name": "Nuki Opener"}],
        entities=[{"entity_id": "lock.nuki", "device_id": "d1"},
                  {"entity_id": "switch.nuki_relais", "device_id": "d1"}],
    )
    assert is_security_entity(idx, "switch.nuki_relais")  # same device as a lock
    assert is_security_entity(idx, "switch.poort_opener")
    assert is_security_entity(idx, "scene.alarm_aan")
    assert is_security_entity(idx, "cover.garage")
    assert not is_security_entity(idx, "switch.outdoor_lights")
    assert not is_security_entity(idx, "switch.deurbel_gong")
    assert not is_security_entity(idx, "light.voordeur")  # a light is never a door
    assert payload_touches_security(idx, "automation", {"config": {"actions": [{"action": "lock.unlock"}]}})
    assert not payload_touches_security(idx, "automation", {"config": {"actions": [{"action": "light.turn_on"}]}})


async def test_agent_never_opens_a_gate_even_with_switch_act(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["switch=act", "lock=act", "alarm_control_panel=act"]
    ctx.index.update_state({"entity_id": "switch.poort_opener", "state": "off",
                            "attributes": {"friendly_name": "Poort opener"}})
    assert ctx.autonomy.level("lock") == "ask"  # capped
    result = await perform(ctx, "switch", "turn_on", ["switch.poort_opener"])
    assert result["status"] == "needs_confirmation"
    assert ("switch", "turn_on", {}, {"entity_id": ["switch.poort_opener"]}) not in ctx.ha.calls
    # A plain switch still works by itself
    result = await perform(ctx, "switch", "turn_on", ["switch.koffiezetapparaat"])
    assert result["status"] == "ok"


async def test_scenes_only_when_released(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["scene=act"]
    ctx.index.services["scene"] = {"turn_on": {}}
    ctx.index.update_state({"entity_id": "scene.film", "state": "x", "attributes": {"friendly_name": "Film"}})
    assert (await perform(ctx, "scene", "turn_on", ["scene.film"]))["status"] == "needs_confirmation"
    ctx.config.autonomous_scenes = ["scene.film"]
    assert (await perform(ctx, "scene", "turn_on", ["scene.film"]))["status"] == "ok"


async def test_chat_asks_before_using_a_door_relay(ctx):
    ctx.index.update_state({"entity_id": "switch.voordeur_opener", "state": "off",
                            "attributes": {"friendly_name": "Voordeur opener"}})
    result = await perform(ctx, "switch", "turn_on", ["switch.voordeur_opener"])
    assert result["status"] == "needs_confirmation"
    assert ctx.confirmations.list()[0].sensitive


def test_confirmations_survive_a_restart(tmp_path):
    db = Database(str(tmp_path))
    store = ConfirmationStore(db)
    store.add(PendingAction("abc", "service", "lock.unlock", "test",
                            {"domain": "lock", "service": "unlock", "entity_ids": ["lock.voordeur"], "data": {}},
                            "c1", sensitive=True))
    again = ConfirmationStore(Database(str(tmp_path)))
    assert again.list()[0].sensitive and again.pop("abc").payload["domain"] == "lock"
    assert ConfirmationStore(Database(str(tmp_path))).list() == []


# --- intrusion without waiting for the LLM ------------------------------------------------------

async def test_door_opening_while_away_is_critical_at_once(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    ctx.index.update_state({"entity_id": "binary_sensor.achterdeur", "state": "off",
                            "attributes": {"friendly_name": "Achterdeur", "device_class": "door"}})
    set_state(ctx.index, "person.vincent", "not_home", 120)
    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.achterdeur", "off", "on"))
    (_, _, data, _), = pushes(ctx)
    assert "Mogelijke inbraak" in data["title"] and data["data"]["priority"] == "high"
    assert ctx.db.incidents(status="active")[0]["severity"] == "critical"


async def test_door_opening_just_after_arrival_is_fine(ctx, notifier):
    ctx.index.update_state({"entity_id": "binary_sensor.achterdeur", "state": "off",
                            "attributes": {"friendly_name": "Achterdeur", "device_class": "door"}})
    set_state(ctx.index, "person.vincent", "not_home", 120)
    watcher = Watcher(ctx, notifier)
    watcher._last_arrival = time.monotonic()
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.achterdeur", "off", "on"))
    assert pushes(ctx) == []


async def test_alarm_triggered_is_critical(ctx, notifier):
    ctx.index.update_state({"entity_id": "alarm_control_panel.huis", "state": "armed_away",
                            "attributes": {"friendly_name": "Alarm"}})
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(event(ctx.index, "alarm_control_panel.huis", "armed_away", "triggered"))
    assert titles(ctx) == ["🚨 Alarm afgegaan"]


async def test_leak_playbook_closes_valve_never_doors(ctx, notifier):
    ctx.config.leak_valve = "valve.hoofdkraan"
    ctx.index.update_state({"entity_id": "valve.hoofdkraan", "state": "open", "attributes": {"friendly_name": "Kraan"}})
    ctx.index.update_state({"entity_id": "binary_sensor.lek_keuken", "state": "off",
                            "attributes": {"friendly_name": "Lek keuken", "device_class": "moisture"}})
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.lek_keuken", "off", "on"))
    assert ("valve", "close_valve", {}, {"entity_id": ["valve.hoofdkraan"]}) in ctx.ha.calls
    assert ctx.db.recent_actions()[0]["reason"].startswith("draaiboek")
    # Smoke: lights on, and a door relay in the fan list is ignored
    ctx.config.emergency_fans_off = ["switch.voordeur_opener"]
    ctx.index.update_state({"entity_id": "switch.voordeur_opener", "state": "on", "attributes": {}})
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.rookmelder_zolder", "off", "on"))
    assert ("light", "turn_on", {}, {"entity_id": ["light.woonkamer_plafond"]}) in ctx.ha.calls
    assert not any(c[0] == "switch" for c in ctx.ha.calls)


# --- who did it, and keeping hands off -------------------------------------------------------------

def test_context_tracker():
    tracker = ContextTracker()
    tracker.remember("c1", AGENT, "test")
    tracker.note_automation({"name": "Avondlicht", "context": {"id": "c2"}})
    assert tracker.classify({"id": "c1", "user_id": "addon"}) == (AGENT, "test")
    assert tracker.classify({"id": "c2"}) == (AUTOMATION, "Avondlicht")
    assert tracker.classify({"id": "x", "parent_id": "p"}) == (AUTOMATION, "")
    assert tracker.classify({"id": "y", "user_id": "vincent"}) == (RESIDENT, "")
    assert tracker.classify({"id": "z", "user_id": "addon"})[0] == "ai"  # our own user, not a resident
    assert tracker.classify({"id": "w"}) == (DEVICE, "")


async def test_resident_reversing_the_agent_blocks_it(ctx, notifier):
    await act_everything(ctx)
    ctx.contexts = ContextTracker()
    agent = AutonomousAgent(ctx, build_autonomous_registry(), ctx.incidents)
    watcher = Watcher(ctx, notifier)
    watcher.agent = agent
    result = await perform(ctx, "light", "turn_on", ["light.woonkamer_plafond"])
    assert result["status"] == "ok"
    await watcher.handle_state_changed(event(ctx.index, "light.woonkamer_plafond", "on", "off",
                                             context={"id": "u1", "user_id": "vincent"}))
    action = ctx.db.recent_actions()[0]
    assert action["feedback"] == -1
    assert "light.woonkamer_plafond" in ctx.db.active_overrides()
    assert any("teruggedraaid" in o for o in agent._observations)
    level, reason = ctx.autonomy.decide("light", "turn_on", ["light.woonkamer_plafond"], {})
    assert level == "ask" and "zelf aangepast" in reason


# --- shadow mode --------------------------------------------------------------------------------

async def test_shadow_mode_records_without_acting(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["climate=shadow"]
    result = await perform(ctx, "climate", "set_temperature", ["climate.thermostaat"], {"temperature": 21})
    assert result["status"] == "shadow" and service_calls(ctx) == []
    assert ctx.db.count_actions(0) == 0
    ctx.db.set_action_feedback(result["action_id"], 1)
    assert ctx.db.shadow_scores() == [{"domain": "climate", "total": 1, "good": 1, "bad": 0}]


# --- verification ----------------------------------------------------------------------------------

async def test_unresponsive_device_is_retried_then_reported(ctx, notifier):
    await act_everything(ctx)
    action_id = ctx.db.add_action("light", "turn_on", ["light.woonkamer_plafond"], {}, {}, "test")
    failed = await verify_action(ctx, action_id, "light", "turn_on", ["light.woonkamer_plafond"], {}, delay=0)
    assert failed == ["light.woonkamer_plafond"]
    assert ctx.ha.calls.count(("light", "turn_on", {}, {"entity_id": ["light.woonkamer_plafond"]})) == 1
    assert ctx.db.get_action(action_id)["verified"] == "failed"
    assert "reageert niet" in pushes(ctx)[-1][2]["message"]
    set_state(ctx.index, "light.woonkamer_plafond", "on")
    assert await verify_action(ctx, action_id, "light", "turn_on", ["light.woonkamer_plafond"], {}, delay=0) == []


# --- the heartbeat runs on its own ------------------------------------------------------------------

async def test_slow_heartbeat_does_not_block_the_scheduler(ctx, notifier):
    await act_everything(ctx)
    started = asyncio.Event()

    class SlowAgent:
        last_summary, last_run_ts = "", 0.0

        def set_mode(self, mode):
            pass

        def due(self):
            return True

        async def run(self):
            started.set()
            await asyncio.sleep(3600)

    scheduler = Scheduler(ctx, notifier, Watcher(ctx, notifier))
    scheduler.agent = SlowAgent()
    await asyncio.wait_for(scheduler.run_once(), 2)
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(scheduler.run_once(), 2)  # the next tick does not wait for the LLM either
    scheduler.stop()


async def test_unreachable_llm_is_reported_once_and_recovery_too(ctx, notifier):
    reachable = {"value": False}

    async def status(url=None):
        return {"reachable": reachable["value"]}

    ctx.llm.status = status
    ctx.config.llm_down_minutes = 5
    scheduler = Scheduler(ctx, notifier, Watcher(ctx, notifier))
    await scheduler._check_llm()
    assert pushes(ctx) == [] and scheduler.health()["ollama"] == "onbereikbaar"
    scheduler.llm_down_since -= 600
    for _ in range(2):
        scheduler._llm_checked = 0
        await scheduler._check_llm()
    assert titles(ctx) == ["⚠️ AI-server niet bereikbaar"]
    reachable["value"] = True
    scheduler._llm_checked = 0
    await scheduler._check_llm()
    assert titles(ctx)[-1] == "✅ AI-server weer bereikbaar"
    assert scheduler.health()["ollama"] == "ok" and scheduler.health()["last_tick"]


async def test_status_sensor_carries_health(ctx, notifier):
    await act_everything(ctx)
    publisher = StatusPublisher(ctx.ha, ctx.db, ctx.autonomy)
    await publisher.publish("thuis", "", {"last_tick": "2026-10-19T12:00+02:00", "ollama": "ok"})
    assert ctx.ha.states_set["sensor.ai_huis_status"][1]["last_tick"] == "2026-10-19T12:00+02:00"


# --- the API key only opens the conversation --------------------------------------------------------

async def test_api_key_only_for_the_conversation():
    async def ok(_request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[access_middleware("secret", supervised=True)])
    app.add_routes([web.post("/api/conversation", ok), web.post("/api/settings", ok)])
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"Authorization": "Bearer secret"}
        assert (await client.post("/api/conversation", headers=headers)).status == 200
        assert (await client.post("/api/settings", headers=headers)).status == 403
        assert (await client.post("/api/conversation")).status == 401
    finally:
        await client.close()


# --- comfort ------------------------------------------------------------------------------------

async def test_comfort_profile_sets_temperature_once(ctx, notifier):
    await act_everything(ctx)
    ctx.config.max_climate_change = 1.0  # a profile is the user's own wish: the LLM limit does not apply
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat",
                          "targets": {"thuis": 21, "slapen": 17}})
    comfort = ComfortController(ctx)
    now = datetime(2026, 10, 19, 23, 30)
    done = await comfort.tick("slapen", now)
    assert done and ("climate", "set_temperature", {"temperature": 17.0},
                     {"entity_id": ["climate.thermostaat"]}) in ctx.ha.calls
    # The resident turns it up by hand: no fight
    calls = len(ctx.ha.calls)
    set_state(ctx.index, "climate.thermostaat", "heat", temperature=19)
    assert await comfort.tick("slapen", now) == [] and len(ctx.ha.calls) == calls
    # A new mode is a new wish
    ctx.db.clear_override("climate.thermostaat")
    assert await comfort.tick("thuis", now)


async def test_comfort_respects_override_and_shadow(ctx, notifier):
    await act_everything(ctx)
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat", "targets": {"thuis": 22}})
    ctx.db.set_override("climate.thermostaat", time.time() + 600)
    comfort = ComfortController(ctx)
    assert await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0)) == []
    ctx.db.clear_override("climate.thermostaat")
    ctx.config.autonomy = ["climate=shadow"]
    await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0))
    assert service_calls(ctx) == [] and ctx.db.shadow_scores()[0]["total"] == 1


async def test_preheat_when_someone_is_on_the_way(ctx, notifier):
    await act_everything(ctx)
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat",
                          "targets": {"thuis": 21, "weg": 16}, "preheat_minutes": 45})
    ctx.index.update_state({"entity_id": "sensor.home_vincent_direction_of_travel", "state": "towards",
                            "attributes": {}})
    ctx.index.update_state({"entity_id": "sensor.home_vincent_distance", "state": "8000",
                            "attributes": {"unit_of_measurement": "m"}})
    comfort = ComfortController(ctx)
    done = await comfort.tick("weg", datetime(2026, 10, 19, 17, 0))
    assert "21 °C" in done[0]
    assert ctx.db.recent_actions()[0]["reason"].startswith("comfortprofiel Woonkamer: voorverwarmen")


async def test_preheat_from_learned_arrivals(ctx):
    for _ in range(3):
        ctx.db.add_arrival(0, 18 * 60)
    comfort = ComfortController(ctx)
    assert "18:00" in comfort.arriving_soon(datetime(2026, 10, 19, 17, 30), 45)  # a Monday
    assert comfort.arriving_soon(datetime(2026, 10, 19, 16, 0), 45) == ""


async def test_ventilation_advice_without_fan(ctx, notifier):
    await act_everything(ctx)
    ctx.index.update_state({"entity_id": "sensor.co2_woonkamer", "state": "1350",
                            "attributes": {"device_class": "carbon_dioxide"}})
    ctx.index._registry["sensor.co2_woonkamer"] = {"area_id": "woonkamer"}
    ctx.index.update_state({"entity_id": "sensor.co2_woonkamer", "state": "1350",
                            "attributes": {"device_class": "carbon_dioxide"}})
    save_profile(ctx.db, {"area": "Woonkamer"})
    comfort = ComfortController(ctx)
    await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0))
    assert titles(ctx)[-1].endswith("Ventileer Woonkamer")
    await comfort.tick("thuis", datetime(2026, 10, 19, 12, 1))
    assert len(pushes(ctx)) == 1


async def test_manual_corrections_become_a_suggestion(ctx, notifier):
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat", "targets": {"thuis": 20}})
    ctx.contexts = ContextTracker()
    watcher = Watcher(ctx, notifier)
    for _ in range(3):
        await watcher.handle_state_changed(event(ctx.index, "climate.thermostaat", "heat", "heat",
                                                 context={"id": "u", "user_id": "vincent"}, temperature=21))
    (suggestion,) = profile_suggestions(ctx.db)
    assert suggestion["value"] == 21 and suggestion["current"] == 20


async def test_chat_tool_turns_a_wish_into_a_confirmed_profile(ctx):
    result = await set_comfort_profile(ctx, "slaapkamer", slapen=17)
    assert result["status"] == "needs_confirmation"
    assert (await resolve_confirmation(ctx, result["confirmation_id"], True))["status"] == "ok"
    (profile,) = ctx.db.profiles()
    assert profile["area"] == "Slaapkamer" and profile["targets"] == {"slapen": 17}


# --- the situation report -------------------------------------------------------------------------

async def test_report_has_rooms_outside_and_origins(ctx, notifier):
    await act_everything(ctx)
    ctx.index.update_state({"entity_id": "sun.sun", "state": "above_horizon",
                            "attributes": {"next_setting": "2026-10-19T16:30:00+00:00"}})
    agent = AutonomousAgent(ctx, build_autonomous_registry(), ctx.incidents)
    agent.note_state_change(ctx.index.get("climate.thermostaat"), "off", "heat", AUTOMATION, "Ochtend")
    report = agent.situation_report([])
    assert "Woonkamer: 21.4 °C" in report
    assert "Zon op, ondergang om 18:30" in report and "Weer: cloudy, 12 °C" in report
    assert "[automatisering: Ochtend]" in report
    ctx.llm = FakeLLM([{"role": "assistant", "content": "Alles rustig"}])
    agent._ctx = agent._ctx.__class__(**{**agent._ctx.__dict__, "llm": ctx.llm})
    agent.observe("test")
    await agent.run()
    assert ctx.db.runs_with_reports()[0]["report"].startswith("Tijd:")


def test_baseline_per_kind_of_day(ctx):
    baseline = Baseline(ctx.db, ctx.index)
    ctx.index.update_state({"entity_id": "sensor.woonkamer_temperatuur", "state": "21.0",
                            "attributes": {"device_class": "temperature", "unit_of_measurement": "°C"}})
    saturday = datetime(2026, 10, 24, 14, 0)
    for i in range(MIN_SAMPLES):
        baseline._last_sample.clear()
        assert baseline.sample(saturday) == []
    assert ctx.db._rows("SELECT n FROM baseline WHERE bucket = 138")[0]["n"] == MIN_SAMPLES


# --- notifications per person ----------------------------------------------------------------------

async def test_doorbell_goes_to_who_is_home_and_digest_bundles(ctx):
    ctx.config.quiet_hours = ""
    ctx.config.person_notify = ["person.vincent=notify.mobile_app_vincent", "person.sanne=notify.mobile_app_sanne"]
    ctx.index.update_state({"entity_id": "person.sanne", "state": "not_home", "attributes": {}})
    notifier = Notifier(ctx.config, ctx.ha, ctx.db)
    notifier.index = ctx.index
    await notifier.send("🔔 Er wordt aangebeld", "Iemand aan de deur", kind="doorbell")
    await notifier.send("🚨 Inbraak", "Achterdeur open", kind="security", critical=True)
    sent = [c[1] for c in pushes(ctx)]
    assert sent == ["mobile_app_vincent", "mobile_app_vincent", "mobile_app_sanne"]
    ctx.config.digest_time = "20:00"
    since = time.time() - 1
    await notifier.send("Ongebruikelijk", "Vriezer is warm", kind="anomaly")
    assert len(pushes(ctx)) == 3
    text = await notifier.send_digest(since)
    assert "Vriezer is warm" in text and pushes(ctx)[-1][2]["title"] == "📋 Overzicht van vandaag"
    assert await notifier.send_digest(since) == ""


def test_verify_delay_is_patched_in_tests():
    assert actions.VERIFY_DELAY == 3600.0


async def test_device_confirming_the_agents_change_is_not_a_reversal(ctx, notifier):
    await act_everything(ctx)
    ctx.contexts = ContextTracker()
    await perform(ctx, "climate", "set_temperature", ["climate.thermostaat"], {"temperature": 21})
    watcher = Watcher(ctx, notifier)
    # A cloud thermostat reports the new setpoint later, without our context
    await watcher.handle_state_changed(event(ctx.index, "climate.thermostaat", "heat", "heat", temperature=21))
    assert ctx.db.recent_actions()[0]["feedback"] is None
    assert ctx.db.active_overrides() == {}


async def test_door_and_camera_alarm_are_one_incident(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    ctx.index.update_state({"entity_id": "alarm_control_panel.huis", "state": "armed_away",
                            "attributes": {"friendly_name": "Alarm"}})
    ctx.index.update_state({"entity_id": "binary_sensor.achterdeur", "state": "off",
                            "attributes": {"friendly_name": "Achterdeur", "device_class": "door"}})
    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.achterdeur", "off", "on"))
    camera = ctx.index.get("camera.voordeur_clear")
    await watcher._camera_alert("Persoon bij Achtertuin terwijl het alarm aan staat", "Iemand in de tuin", camera,
                                None, severity="critical", critical=True)
    assert len(pushes(ctx)) == 1
    (incident,) = ctx.db.incidents(status="active")
    assert len(ctx.db.get_incident(incident["id"])["events"]) == 2
