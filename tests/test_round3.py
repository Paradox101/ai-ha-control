import time

from app.watcher.incidents import IncidentManager
from app.watcher.rules import Watcher

from .test_security_comfort import pushes


def actions_of(call):
    return [a["action"] for a in call[2]["data"].get("actions", [])]


# --- critical notifications: "seen" and escalation -------------------------------------------------

async def test_unseen_critical_notification_is_repeated_and_announced(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_vincent"
    ctx.config.tts_entity, ctx.config.announce_players = "tts.piper", ["media_player.woonkamer"]
    incidents = IncidentManager(ctx.db, notifier)
    incident_id = await incidents.report("safety:rook", "Alarm: rook", "Rookmelder zolder detecteert rook!",
                                         severity="critical", critical=True)
    first = pushes(ctx)[-1]
    event_id = ctx.db.events()[0]["id"]
    assert actions_of(first)[0] == f"AIHA_ACK_{event_id}"
    assert await notifier.escalate() == 0  # not due yet

    later = time.time() + 6 * 60
    assert await notifier.escalate(later) == 1
    assert pushes(ctx)[-1][2]["title"].startswith("‼️ Nog niet gezien")
    assert any(c[0] == "tts" for c in ctx.ha.calls)
    assert ctx.db.incidents(status="active")[0]["unseen"] == 1
    assert "opnieuw gemeld" in ctx.db.get_incident(incident_id)["events"][-1]["text"]

    # at most three times
    assert await notifier.escalate(later + 600) == 1
    assert await notifier.escalate(later + 1200) == 1
    assert await notifier.escalate(later + 1800) == 0


async def test_seen_on_the_phone_stops_escalation(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    incident_id = await incidents.report("security", "Mogelijke inbraak", "Achterdeur ging open",
                                         severity="critical", critical=True)
    event_id = ctx.db.events()[0]["id"]
    watcher = Watcher(ctx, notifier)
    await watcher.handle_notification_action({"action": f"AIHA_ACK_{event_id}"})
    assert await notifier.escalate(time.time() + 3600) == 0
    assert ctx.db.incidents(status="active")[0]["unseen"] == 0
    assert ctx.db.get_incident(incident_id)["events"][-1]["text"] == "Gezien via telefoon"


async def test_resolved_incident_or_feedback_stops_escalation(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    first = await incidents.report("security", "Inbraak", "Deur open", severity="critical", critical=True)
    incidents.resolve(first)
    await notifier.send("⚠️ Alarm: lekkage", "Lekkage in de keuken", kind="safety", critical=True)
    leak = ctx.db.events()[0]["id"]
    await Watcher(ctx, notifier).handle_notification_action({"action": f"AIHA_FB_UP_{leak}"})
    assert await notifier.escalate(time.time() + 3600) == 0


async def test_escalation_off_and_normal_notifications(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_vincent"
    await notifier.send("Raam open", "Slaapkamer", kind="energy")
    assert not any(a.startswith("AIHA_ACK_") for a in actions_of(pushes(ctx)[-1]))
    ctx.config.escalate_minutes = 0
    await notifier.send("🚨 Inbraak", "Deur open", kind="security", critical=True)
    assert not any(a.startswith("AIHA_ACK_") for a in actions_of(pushes(ctx)[-1]))
    assert await notifier.escalate(time.time() + 3600) == 0


# --- watch rules without the LLM ----------------------------------------------------------------------

from app.tools.actions import resolve_confirmation  # noqa: E402
from app.watcher.thresholds import ThresholdMonitor, add_watch_rule, matches  # noqa: E402

from .test_security_comfort import set_state  # noqa: E402
from .test_settings_api import client  # noqa: E402,F401


def test_matches():
    assert matches("-12.5", ">", "-15") is True
    assert matches("-18", ">", "-15") is False
    assert matches("on", "=", "on") is True and matches("on", "!=", "on") is False
    assert matches("unavailable", ">", "-15") is None
    assert matches("open", "<", "3") is None


async def watch_setup(ctx, notifier):
    ctx.index.update_state({"entity_id": "sensor.vriezer", "state": "-18",
                            "attributes": {"friendly_name": "Vriezer", "device_class": "temperature",
                                           "unit_of_measurement": "°C"}})
    result = await add_watch_rule(ctx, "sensor.vriezer", ">", "-15", minutes=10)
    assert result["status"] == "needs_confirmation"
    assert ctx.db.watch_rules() == []  # nothing until confirmed
    assert (await resolve_confirmation(ctx, result["confirmation_id"], True))["status"] == "ok"
    return ThresholdMonitor(ctx, IncidentManager(ctx.db, notifier))


async def test_watch_rule_fires_after_duration_and_recovers(ctx, notifier):
    monitor = await watch_setup(ctx, notifier)
    now = time.time()
    set_state(ctx.index, "sensor.vriezer", "-12")
    await monitor.check(ctx.index.get("sensor.vriezer"), now)
    assert ctx.db.incidents(status="active") == []  # not yet ten minutes
    await monitor.tick(now=now + 11 * 60)
    active = ctx.db.incidents(status="active")
    assert len(active) == 1 and "Vriezer is -12 °C" in active[0]["summary"]
    await monitor.tick(now=now + 20 * 60)
    assert len(ctx.db.events()) == 1  # no repeat while it stays too warm

    set_state(ctx.index, "sensor.vriezer", "-17")
    await monitor.check(ctx.index.get("sensor.vriezer"), now + 30 * 60)
    assert ctx.db.incidents(status="active") == []
    assert ctx.db.events()[0]["title"] == "✅ Weer normaal"


async def test_watch_rule_only_in_mode_and_validation(ctx, notifier):
    monitor = ThresholdMonitor(ctx, IncidentManager(ctx.db, notifier))
    bad = await add_watch_rule(ctx, "sensor.bestaat_niet", ">", "1")
    assert bad["status"] == "error"
    assert (await add_watch_rule(ctx, "lock.voordeur", ">", "open"))["status"] == "error"
    ctx.db.add_watch_rule({"entity_id": "lock.voordeur", "op": "=", "value": "unlocked", "modes": ["weg"]})
    set_state(ctx.index, "lock.voordeur", "unlocked")
    await monitor.tick(mode="thuis")
    assert ctx.db.incidents(status="active") == []
    await monitor.tick(mode="weg")
    assert len(ctx.db.incidents(status="active")) == 1


async def test_watch_rules_and_ack_api(client):
    bad = await client.post("/api/watch-rules", json={"entity_id": "sensor.woonkamer_temperatuur", "operator": ">"})
    assert bad.status == 400
    rules = await (await client.post("/api/watch-rules", json={
        "entity_id": "sensor.woonkamer_temperatuur", "operator": ">", "value": "25", "minutes": 5})).json()
    assert len(rules) == 1 and "boven 25" in rules[0]["text"]
    rules = await (await client.post(f"/api/watch-rules/{rules[0]['id']}/toggle", json={"enabled": False})).json()
    assert rules[0]["enabled"] is False
    assert await (await client.delete(f"/api/watch-rules/{rules[0]['id']}")).json() == []

    state = client.app["state"]
    incident_id = await state.incidents.report("security", "Inbraak", "Deur open", severity="critical", critical=True)
    listed = await (await client.get("/api/incidents?status=active")).json()
    assert listed[0]["unseen"] == 1
    assert (await (await client.post(f"/api/incidents/{incident_id}/ack")).json())["stopped"] == 1
    assert (await (await client.get("/api/incidents?status=active")).json())[0]["unseen"] == 0


# --- bedtime round ---------------------------------------------------------------------------------------

async def test_bedtime_round_lists_and_turns_lights_off(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_vincent"
    set_state(ctx.index, "lock.voordeur", "unlocked")
    watcher = Watcher(ctx, notifier)
    text = await watcher.bedtime_round()
    assert "Raam slaapkamer" in text and "Voordeur slot" in text and "Keukenlamp" in text
    bedtime = next(c for c in pushes(ctx) if c[2]["title"] == "🌙 Nachtronde")
    (button,) = [a for a in actions_of(bedtime) if a.startswith("AIHA_LIGHTS_OFF_")]
    assert button == f"AIHA_LIGHTS_OFF_{ctx.db.events(limit=10)[-1]['id']}"
    assert [p.payload["entity_ids"] for p in ctx.confirmations.list()] == [["lock.voordeur"]]
    assert not any(c[0] == "lock" for c in ctx.ha.calls)  # never by itself

    await watcher.handle_notification_action({"action": button})
    assert ("light", "turn_off") in [(c[0], c[1]) for c in ctx.ha.calls]
    off = next(c for c in ctx.ha.calls if c[:2] == ("light", "turn_off"))
    assert off[3] == {"entity_id": ["light.keuken"]}


async def test_bedtime_round_quiet_when_all_is_well(ctx, notifier):
    set_state(ctx.index, "binary_sensor.raam_slaapkamer", "off")
    set_state(ctx.index, "light.keuken", "off")
    assert await Watcher(ctx, notifier).bedtime_round() == ""
    assert pushes(ctx) == []


async def test_scheduler_runs_bedtime_round_on_entering_sleep(ctx, notifier, monkeypatch):
    from app.watcher import scheduler as scheduler_module
    from app.watcher.scheduler import Scheduler

    watcher = Watcher(ctx, notifier)
    calls = []

    async def fake_round():
        calls.append(1)
        return ""

    watcher.bedtime_round = fake_round
    sched = Scheduler(ctx, notifier, watcher)
    modes = iter(["thuis", "slapen", "slapen", "thuis", "slapen"])
    monkeypatch.setattr(scheduler_module, "house_mode", lambda *a: next(modes))
    for _ in range(5):
        await sched._autonomous_round(notifier.now())
    assert len(calls) == 2


# --- reminders and planned actions ----------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402

from app.scheduled import parse_when, remind_me, run_due, schedule_action  # noqa: E402


def test_parse_when(ctx):
    now = datetime(2026, 10, 19, 20, 0)
    assert parse_when(ctx, at="18:00", now=now) == datetime(2026, 10, 20, 18, 0)
    assert parse_when(ctx, at="21:15", now=now) == datetime(2026, 10, 19, 21, 15)
    assert parse_when(ctx, in_minutes=30, now=now) == now + timedelta(minutes=30)
    assert isinstance(parse_when(ctx, at="straks", now=now), str)
    assert isinstance(parse_when(ctx, now=now), str)
    assert isinstance(parse_when(ctx, in_minutes=60 * 24 * 40, now=now), str)


async def test_reminder_is_sent_when_due(ctx, notifier):
    result = await remind_me(ctx, "Vuilnisbak buiten zetten", in_minutes=30)
    assert result["status"] == "scheduled"
    assert await run_due(ctx) == 0
    assert await run_due(ctx, time.time() + 31 * 60) == 1
    assert ctx.db.events()[0]["title"] == "⏰ Herinnering"
    assert ctx.db.scheduled() == []


async def test_planned_action_runs_through_policy(ctx, notifier):
    light = await schedule_action(ctx, "light", "turn_off", ["light.keuken"], in_minutes=60)
    lock = await schedule_action(ctx, "lock", "unlock", ["lock.voordeur"], in_minutes=90)
    assert light["status"] == lock["status"] == "scheduled"
    assert (await schedule_action(ctx, "light", "turn_off", ["light.bestaat_niet"], in_minutes=5))["status"] == "error"
    assert await run_due(ctx, time.time() + 2 * 3600) == 2
    assert ("light", "turn_off") in [c[:2] for c in ctx.ha.calls]
    assert not any(c[0] == "lock" for c in ctx.ha.calls)  # doors are still asked, not done
    assert [p.payload["entity_ids"] for p in ctx.confirmations.list()] == [["lock.voordeur"]]


async def test_missed_action_is_not_run_late(ctx, notifier):
    await schedule_action(ctx, "light", "turn_off", ["light.keuken"], in_minutes=5)
    await run_due(ctx, time.time() + 86400)
    assert not any(c[:2] == ("light", "turn_off") for c in ctx.ha.calls)
    assert ctx.db.events()[0]["title"] == "⚙️ Geplande actie overgeslagen"


# --- appliances done and water that keeps running -------------------------------------------------------

from app.watcher.appliances import ApplianceMonitor  # noqa: E402


def power(ctx, watts, entity_id="sensor.wasmachine_vermogen"):
    ctx.index.update_state({"entity_id": entity_id, "state": str(watts),
                            "attributes": {"friendly_name": "Wasmachine vermogen", "device_class": "power",
                                           "unit_of_measurement": "W"}})


async def test_washer_done_after_a_real_run(ctx, notifier):
    monitor = ApplianceMonitor(ctx, notifier)
    t = time.time()
    power(ctx, 0)
    await monitor.tick("thuis", t)
    for minute, watts in [(1, 2000), (20, 150), (40, 3), (45, 600), (60, 2), (65, 1)]:
        power(ctx, watts)
        await monitor.tick("thuis", t + minute * 60)
    assert ctx.db.events() == []  # low for only 5 minutes
    await monitor.tick("thuis", t + 71 * 60)
    (event,) = ctx.db.events()
    assert event["title"] == "🧺 Wasmachine is klaar" and "59 minuten" in event["message"]
    await monitor.tick("thuis", t + 90 * 60)
    assert len(ctx.db.events()) == 1


async def test_short_blip_is_not_a_program(ctx, notifier):
    monitor = ApplianceMonitor(ctx, notifier)
    t = time.time()
    for minute, watts in [(0, 0), (1, 50), (3, 1), (20, 1)]:
        power(ctx, watts)
        await monitor.tick("thuis", t + minute * 60)
    assert ctx.db.events() == []


def meter(ctx, liters):
    ctx.index.update_state({"entity_id": "sensor.watermeter", "state": str(liters),
                            "attributes": {"friendly_name": "Watermeter", "device_class": "water",
                                           "state_class": "total_increasing", "unit_of_measurement": "L"}})


async def test_water_running_all_night_is_a_leak(ctx, notifier):
    monitor = ApplianceMonitor(ctx, notifier, IncidentManager(ctx.db, notifier))
    t = time.time()
    liters = 1000.0
    for minute in range(0, 3 * 60, 5):
        liters += 0.5
        meter(ctx, liters)
        await monitor.tick("slapen", t + minute * 60)
    (incident,) = ctx.db.incidents(status="active")
    assert incident["title"] == "Water blijft lopen"


async def test_normal_use_with_pauses_is_no_leak(ctx, notifier):
    monitor = ApplianceMonitor(ctx, notifier, IncidentManager(ctx.db, notifier))
    t = time.time()
    liters = 1000.0
    for minute in range(0, 5 * 60, 5):
        if minute % 60 < 20:  # 20 minutes of use per hour, then a rest
            liters += 3
        meter(ctx, liters)
        await monitor.tick("slapen", t + minute * 60)
    assert ctx.db.incidents(status="active") == []


# --- home doctor ----------------------------------------------------------------------------------------

from app.doctor import format_report, health_findings, send_health_report  # noqa: E402

from .test_security_comfort import ago  # noqa: E402


async def test_home_doctor_finds_broken_things(ctx, notifier):
    def add(entity_id, state, minutes=0, **attrs):
        ctx.index.update_state({"entity_id": entity_id, "state": state, "attributes": attrs,
                                "last_changed": ago(minutes)})

    add("automation.vertrek", "on", friendly_name="Vertrek", id="1", last_triggered=ago(5))
    add("automation.kerst", "on", 200 * 1440, friendly_name="Kerstverlichting", id="2", last_triggered=ago(90 * 1440))
    add("automation.oud", "off", friendly_name="Oude regel")
    add("automation.kapot", "unavailable", friendly_name="Kapotte regel")

    async def states():
        return [{"entity_id": "update.home_assistant_core_update", "state": "on",
                 "attributes": {"friendly_name": "Home Assistant Core", "installed_version": "2026.9.1",
                                "latest_version": "2026.10.2"}}]

    ctx.ha.get_states = states
    add("sensor.tuin_temperatuur", "unavailable", 5 * 1440, friendly_name="Tuin temperatuur")
    add("sensor.system_monitor_disk_use_percent", "91", friendly_name="Schijfgebruik")

    async def config(automation_id):
        return {"triggers": [{"trigger": "state", "entity_id": "sensor.tuin_temperatuur"}],
                "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.weg"}}]} \
            if automation_id == "1" else {"actions": []}

    ctx.ha.get_automation_config = config
    findings = await health_findings(ctx)
    assert findings["Automatiseringen met een fout"] == ["Kapotte regel"]
    assert findings["Automatiseringen die uit staan"] == ["Oude regel"]
    assert findings["Automatiseringen al 60+ dagen niet uitgevoerd"] == ["Kerstverlichting (90 dagen)"]
    (broken,) = findings["Automatiseringen die naar kapotte apparaten verwijzen"]
    assert "light.weg bestaat niet" in broken and "Tuin temperatuur al 5 dagen onbereikbaar" in broken
    assert findings["Updates beschikbaar"] == ["Home Assistant Core (2026.9.1 → 2026.10.2)"]
    assert "Tuin temperatuur" in findings["Al 3+ dagen onbereikbaar"]
    assert findings["Schijf bijna vol"] == ["Schijfgebruik: 91% vol"]
    assert "Kapotte regel" in await send_health_report(ctx)
    assert ctx.db.events()[0]["title"] == "🩺 Huisdokter"


async def test_healthy_home_sends_nothing(ctx, notifier):
    assert format_report({}).startswith("Alles gezond")
    assert await send_health_report(ctx) == ""


# --- presence simulation --------------------------------------------------------------------------------

from app.watcher.simulation import PresenceSimulator  # noqa: E402


async def test_presence_simulation_follows_learned_use(ctx, notifier):
    ctx.config.presence_simulation = True
    set_state(ctx.index, "light.keuken", "off")
    ctx.index.update_state({"entity_id": "sun.sun", "state": "below_horizon", "attributes": {}})
    ctx.index.update_state({"entity_id": "binary_sensor.keuken_beweging", "state": "off",
                            "attributes": {"friendly_name": "Beweging keuken", "device_class": "motion"}})
    ctx.index.entities["binary_sensor.keuken_beweging"].area = "Keuken"
    for hour, count in [(19, 40), (20, 30), (21, 5)]:
        for _ in range(count):
            ctx.db.activity_increment("binary_sensor.keuken_beweging", hour)
    sim = PresenceSimulator(ctx)
    evening = next(datetime(2026, 10, d, 19, 50) for d in range(1, 29)
                   if sim.wanted("Keuken", datetime(2026, 10, d, 19, 50)))

    await sim.tick("thuis", evening)
    assert ctx.ha.calls == []  # only on vacation
    await sim.tick("vakantie", evening)
    assert ("light", "turn_on", {}, {"entity_id": ["light.keuken"]}) in ctx.ha.calls
    set_state(ctx.index, "light.keuken", "on")
    calls = len(ctx.ha.calls)
    await sim.tick("vakantie", evening)
    assert len(ctx.ha.calls) == calls  # no repeats
    await sim.tick("vakantie", evening.replace(hour=23, minute=50) + timedelta(hours=1))
    assert ("light", "turn_off", {}, {"entity_id": ["light.keuken"]}) in ctx.ha.calls


def test_presence_simulation_hours(ctx):
    sim = PresenceSimulator(ctx)
    ctx.db.activity_increment("binary_sensor.x", 20)
    ctx.index.update_state({"entity_id": "binary_sensor.x", "state": "off",
                            "attributes": {"friendly_name": "X", "device_class": "motion"}})
    ctx.index.entities["binary_sensor.x"].area = "Woonkamer"
    days = [datetime(2026, 10, d, 20, 30) for d in range(1, 29)]
    used = sum(sim.in_use("Woonkamer", d) for d in days)
    assert 18 <= used < 28  # mostly, but not every evening
    assert not any(sim.in_use("Woonkamer", d.replace(hour=15)) for d in days)
    assert sim.in_use("Zolder", datetime(2026, 10, 1, 20, 30)) is False  # no data, no living room
    assert not sim.wanted("Woonkamer", datetime(2026, 10, 2, 1, 30))
