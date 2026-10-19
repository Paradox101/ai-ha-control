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
