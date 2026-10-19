"""Fixes from the code review (0.6.18): doors and alarm, undo, notifications, safety sensors, simulation."""

import time
from datetime import datetime, timedelta

from app.autonomy import restore_calls, undo_action
from app.comfort import ComfortController, save_profile
from app.ha.index import EntityIndex
from app.security import is_security_entity, payload_touches_security
from app.tools.actions import perform, verify_action
from app.watcher.incidents import IncidentManager
from app.watcher.rules import Watcher
from app.watcher.simulation import PresenceSimulator

from .test_security_comfort import act_everything, cv_profile, cv_setpoints, event, pushes, service_calls, set_state


# --- doors and the alarm --------------------------------------------------------------------

async def test_targets_hidden_in_data_are_refused(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["switch=act"]
    for data in ({"device_id": "garage_relais"}, {"area_id": "garage"}, {"label_id": "x"}):
        result = await perform(ctx, "switch", "turn_on", ["switch.koffiezetapparaat"], data)
        assert result["status"] == "error" and "entity_ids" in result["error"]
    ctx.source = "web"
    result = await perform(ctx, "scene", "apply", [], {"entities": {"lock.voordeur": "unlocked"}})
    assert result["status"] == "error"
    assert service_calls(ctx) == []


def test_service_payloads_that_hide_their_target_are_sensitive(index):
    def service(domain, service_name, entity_ids=(), data=None):
        return payload_touches_security(index, "service", {"domain": domain, "service": service_name,
                                                           "entity_ids": list(entity_ids), "data": data or {}})

    assert service("cover", "open_cover", data={"area_id": "garage"})
    assert service("light", "turn_on")  # no entity at all
    assert service("script", "garage_openen")
    assert service("homeassistant", "turn_on", ["light.keuken"], {"note": "lock.voordeur"})
    assert not service("light", "turn_on", ["light.keuken"], {"brightness": 100})
    assert not service("notify", "mobile_app_telefoon")


def test_automations_with_device_actions_or_area_targets_are_sensitive(index):
    unlock = {"device_id": "abc", "domain": "lock", "type": "unlock", "entity_id": "0f1e2d"}
    assert payload_touches_security(index, "automation", {"config": {"actions": [unlock]}})
    area = {"action": "cover.open_cover", "target": {"area_id": "garage"}}
    assert payload_touches_security(index, "automation", {"config": {"actions": [area]}})
    motion = {"trigger": "device", "device_id": "m1", "domain": "binary_sensor", "type": "motion"}
    light = {"action": "light.turn_on", "target": {"entity_id": "light.keuken"}}
    assert not payload_touches_security(index, "automation", {"config": {"triggers": [motion], "actions": [light]}})


def test_scenes_and_groups_are_judged_by_their_members():
    idx = EntityIndex()
    idx.load([
        {"entity_id": "lock.voordeur", "state": "locked", "attributes": {}},
        {"entity_id": "scene.vertrek", "state": "x", "attributes": {"entity_id": ["light.hal", "lock.voordeur"]}},
        {"entity_id": "switch.groep_buiten", "state": "off", "attributes": {"entity_id": ["scene.vertrek"]}},
        {"entity_id": "scene.lus", "state": "x", "attributes": {"entity_id": ["scene.lus", "light.hal"]}},
        {"entity_id": "scene.avond", "state": "x", "attributes": {"entity_id": ["light.hal"]}},
    ])
    assert is_security_entity(idx, "scene.vertrek")
    assert is_security_entity(idx, "switch.groep_buiten")
    assert not is_security_entity(idx, "scene.lus")  # a scene listing itself does not loop forever
    assert not is_security_entity(idx, "scene.avond")


# --- verification, undo ----------------------------------------------------------------------

async def test_verify_does_not_retry_against_the_resident(ctx, notifier):
    await act_everything(ctx)
    action_id = ctx.db.add_action("light", "turn_on", ["light.woonkamer_plafond"], {}, {}, "test")
    ctx.db.set_override("light.woonkamer_plafond", time.time() + 600, "aangepast door bewoner")
    assert await verify_action(ctx, action_id, "light", "turn_on", ["light.woonkamer_plafond"], {}, delay=0) == []
    assert service_calls(ctx) == []
    assert ctx.db.get_action(action_id)["verified"] == "overridden"


async def test_shadow_actions_cannot_be_undone(ctx, notifier):
    await act_everything(ctx)
    action_id = ctx.db.add_action("light", "turn_on", ["light.keuken"], {}, {"light.keuken": {"state": "off"}},
                                  "test", shadow=True)
    result = await undo_action(ctx, action_id)
    assert result["status"] == "error" and service_calls(ctx) == []


def test_undo_restores_playback_and_modes():
    playing = restore_calls("media_player.tv", {"state": "playing", "attributes": {"volume_level": 0.3}})
    assert ("media_player", "media_play", {}) in playing
    assert ("media_player", "volume_set", {"volume_level": 0.3}) in playing
    assert restore_calls("media_player.tv", {"state": "paused", "attributes": {}}) == [
        ("media_player", "media_pause", {})]
    climate = restore_calls("climate.airco", {"state": "cool", "attributes": {
        "hvac_modes": ["off", "cool"], "temperature": 22, "preset_mode": "eco", "fan_mode": "low"}})
    assert ("climate", "set_preset_mode", {"preset_mode": "eco"}) in climate
    assert ("climate", "set_fan_mode", {"fan_mode": "low"}) in climate


# --- notifications ---------------------------------------------------------------------------

async def test_one_broken_phone_does_not_silence_the_others(ctx, notifier):
    ctx.config.person_notify = ["person.a=notify.oud", "person.b=notify.nieuw"]
    real = ctx.ha.call_service

    async def call_service(domain, service, data=None, target=None, return_response=False):
        if service == "oud":
            raise RuntimeError("service not found")
        return await real(domain, service, data, target, return_response)

    ctx.ha.call_service = call_service
    await notifier.send("Rook", "Rook in de keuken", kind="safety", critical=True)
    assert [c[1] for c in pushes(ctx)] == ["nieuw"]


async def test_digest_keeps_items_behind_many_other_events(ctx, notifier):
    ctx.db.add_event("energy", "Sluipverbruik", "Vriezer verbruikt veel")
    for i in range(80):
        ctx.db.add_event("detection", "Voorbijganger", f"{i}", pushed=True)
    text = await notifier.send_digest(time.time())  # e.g. right after a restart
    assert "Sluipverbruik" in text


# --- safety sensors --------------------------------------------------------------------------

async def test_safety_sensor_waking_up_in_alarm_is_heard(ctx, notifier):
    ctx.index.update_state({"entity_id": "binary_sensor.rook_zolder", "state": "unavailable",
                            "attributes": {"friendly_name": "Rook zolder", "device_class": "smoke"}})
    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    await watcher.handle_state_changed(event(ctx.index, "binary_sensor.rook_zolder", "unavailable", "on"))
    alerts = [c for c in pushes(ctx) if c[2]["message"] == "Rook zolder detecteert rook!"]
    assert len(alerts) == 1
    count = len(pushes(ctx))
    watcher._grace_until = 0
    await watcher.tick()
    assert len(pushes(ctx)) == count  # the periodic check does not repeat it


async def test_safety_sensor_already_on_at_start_is_reported_once(ctx, notifier):
    set_state(ctx.index, "binary_sensor.rook_zolder", "on", minutes_ago=5,
              friendly_name="Rook zolder", device_class="smoke")
    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    watcher._grace_until = 0
    await watcher.tick()
    await watcher.tick()
    assert sum(c[2]["message"] == "Rook zolder detecteert rook!" for c in pushes(ctx)) == 1


# --- presence simulation -----------------------------------------------------------------------

async def test_simulation_turns_its_lights_off_when_it_stops_while_away(ctx, notifier):
    await act_everything(ctx)
    simulation = PresenceSimulator(ctx)
    simulation._on.add("light.keuken")
    await simulation.tick("weg", None)
    assert ("light", "turn_off", {}, {"entity_id": ["light.keuken"]}) in ctx.ha.calls
    simulation._on.add("light.keuken")
    ctx.ha.calls.clear()
    await simulation.tick("thuis", None)
    assert service_calls(ctx) == []  # back home: the residents decide


# --- comfort ---------------------------------------------------------------------------------

async def test_boiler_backs_off_when_the_airco_is_switched_off(ctx, notifier):
    await act_everything(ctx)
    cv_profile(ctx, below=-3)
    set_state(ctx.index, "weather.thuis", "snowy", temperature=-5)
    comfort = ComfortController(ctx)
    now = datetime(2026, 12, 1, 8, 0)
    await comfort.tick("thuis", now)
    assert cv_setpoints(ctx) == [21.0]
    set_state(ctx.index, "climate.cv", "heat", temperature=21)
    set_state(ctx.index, "climate.thermostaat", "off")
    await comfort.tick("thuis", now + timedelta(minutes=1))
    assert cv_setpoints(ctx) == [21.0, 15.0] and comfort.heating[1]["source"] == "uit"


async def test_failed_setpoint_is_tried_again(ctx, notifier):
    await act_everything(ctx)
    cv_profile(ctx, below=-3)
    set_state(ctx.index, "weather.thuis", "snowy", temperature=-5)
    real = ctx.ha.call_service
    failing = {"on": True}

    async def call_service(domain, service, data=None, target=None, return_response=False):
        if failing["on"] and (domain, service) == ("climate", "set_temperature"):
            raise RuntimeError("Home Assistant niet bereikbaar")
        return await real(domain, service, data, target, return_response)

    ctx.ha.call_service = call_service
    comfort = ComfortController(ctx)
    now = datetime(2026, 12, 1, 8, 0)
    await comfort.tick("thuis", now)
    assert cv_setpoints(ctx) == []
    failing["on"] = False
    await comfort.tick("thuis", now + timedelta(minutes=1))
    assert cv_setpoints(ctx) == [21.0]


async def test_dusk_lights_not_after_midnight(ctx, notifier):
    await act_everything(ctx)
    save_profile(ctx.db, {"area": "Woonkamer", "lights": ["light.woonkamer_plafond"]})
    ctx.index.update_state({"entity_id": "sun.sun", "state": "below_horizon", "attributes": {"rising": False}})
    comfort = ComfortController(ctx)
    assert await comfort.tick("thuis", datetime(2026, 6, 21, 0, 30)) == []
    assert await comfort.tick("thuis", datetime(2026, 6, 21, 22, 30))
