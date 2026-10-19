from datetime import datetime, timedelta, timezone

from app.notify import in_quiet_hours
from app.watcher.rules import Watcher
from app.watcher.scheduler import Scheduler, low_batteries

from .conftest import FakeLLM


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def set_state(index, entity_id, state, minutes_ago=0, **attrs):
    ent = index.get(entity_id)
    index.update_state({
        "entity_id": entity_id,
        "state": state,
        "attributes": {**ent.attributes, **attrs},
        "last_changed": ago(minutes_ago),
    })


def turn_on_event(index, entity_id):
    set_state(index, entity_id, "on")
    return {"entity_id": entity_id, "old_state": {"state": "off"}, "new_state": {"entity_id": entity_id, "state": "on"}}


def pushes(ctx):
    return [c for c in ctx.ha.calls if c[0] in ("notify", "persistent_notification")]


def test_quiet_hours():
    night = datetime(2026, 10, 19, 23, 30)
    day = datetime(2026, 10, 19, 12, 0)
    assert in_quiet_hours("23:00-07:00", night)
    assert not in_quiet_hours("23:00-07:00", day)
    assert in_quiet_hours("12:00-13:00", day)
    assert not in_quiet_hours("", night)


async def test_safety_alert_is_critical(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.rookmelder_zolder"))
    (domain, service, data, _), = pushes(ctx)
    assert (domain, service) == ("notify", "mobile_app_telefoon")
    assert "rook" in data["message"]
    assert data["data"]["priority"] == "high"
    # cooldown: no second push
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.rookmelder_zolder"))
    assert len(pushes(ctx)) == 1


async def test_camera_detection_only_when_away(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.voordeur_person"))
    await watcher.wait_idle()
    assert pushes(ctx) == []  # someone is home

    set_state(ctx.index, "person.vincent", "not_home", 60)
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.voordeur_person"))
    await watcher.wait_idle()
    (_, _, data, _), = pushes(ctx)
    assert "pakketje" in data["message"]
    assert ctx.db.events()[0]["entity_id"] == "camera.voordeur_clear"  # clear stream preferred


async def test_camera_nothing_seen_is_not_pushed(ctx, notifier):
    async def nothing(image, question):
        return "NIETS: alleen een lege oprit."
    ctx.llm.describe_image = nothing
    ctx.config.camera_alerts = "always"
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.voordeur_person"))
    await watcher.wait_idle()
    assert pushes(ctx) == []
    assert ctx.db.events()[0]["pushed"] == 0


async def test_doorbell_always_pushes(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    await watcher.handle_state_changed(turn_on_event(ctx.index, "binary_sensor.voordeur_visitor"))
    await watcher.wait_idle()
    (_, _, data, _), = pushes(ctx)
    assert "aangebeld" in data["title"]


async def test_window_open_while_heating(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    set_state(ctx.index, "climate.thermostaat", "heat", hvac_action="heating")
    set_state(ctx.index, "binary_sensor.raam_slaapkamer", "on", minutes_ago=3)
    await watcher.tick()
    assert pushes(ctx) == []
    set_state(ctx.index, "binary_sensor.raam_slaapkamer", "on", minutes_ago=15)
    await watcher.tick()
    await watcher.tick()
    (_, _, data, _), = pushes(ctx)
    assert "Raam slaapkamer" in data["message"]


async def test_unsecured_while_away_asks_confirmation(ctx, notifier):
    ctx.config.notify_service = "notify.mobile_app_telefoon"
    watcher = Watcher(ctx, notifier)
    set_state(ctx.index, "person.vincent", "not_home", 30)
    set_state(ctx.index, "cover.garagedeur", "open", 40)
    await watcher.tick()
    await watcher.tick()
    (_, _, data, _), = pushes(ctx)
    action = data["data"]["actions"][0]["action"]
    assert action.startswith("AIHA_CONFIRM_")
    # User presses "Uitvoeren" in the Companion app
    await watcher.handle_notification_action({"action": action})
    assert ("cover", "close_cover", {}, {"entity_id": ["cover.garagedeur"]}) in ctx.ha.calls


async def test_daily_summary_and_batteries(ctx, notifier):
    ctx.index.update_state({"entity_id": "sensor.deur_batterij", "state": "8",
                            "attributes": {"friendly_name": "Deursensor batterij", "device_class": "battery"}})
    assert low_batteries(ctx) == ["Deursensor batterij (8%)"]
    ctx.llm = FakeLLM([{"role": "assistant", "content": "Goedemorgen! Vervang de batterij van de deursensor."}])
    scheduler = Scheduler(ctx, notifier, Watcher(ctx, notifier))
    text = await scheduler.daily_summary()
    assert "batterij" in text
    assert "Deursensor batterij (8%)" in ctx.llm.requests[0]["messages"][1]["content"]
    assert ctx.db.events()[0]["kind"] == "summary"


def test_scheduler_due(ctx, notifier):
    scheduler = Scheduler(ctx, notifier, Watcher(ctx, notifier))
    at = datetime(2026, 10, 19, 7, 31)
    assert scheduler._due("summary", "07:30", at)
    assert not scheduler._due("summary", "07:30", at)  # once per day
    assert not scheduler._due("other", "07:30", datetime(2026, 10, 19, 7, 0))
    assert not scheduler._due("late", "07:30", datetime(2026, 10, 19, 12, 0))  # missed: skip today
    assert not scheduler._due("off", "", at)
