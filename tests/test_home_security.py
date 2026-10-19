"""Home security rules over the alarm, cameras and Ajax sensors: report only, never arm or unlock."""

from datetime import datetime, timedelta, timezone

from app.watcher.guard import SecurityGuard, alarm_links, security_answer, tamper_kind
from app.watcher.incidents import IncidentManager
from app.watcher.rules import Watcher
from app.watcher.scheduler import Scheduler
from app.watcher.signals import HouseSignals
from app.watcher.vision import SUSPICIOUS, VISITOR

from .test_house_entities import put
from .test_security_comfort import service_calls, titles

NOW = datetime(2026, 10, 19, 14, 0)


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def away(ctx, minutes=30):
    put(ctx, "person.vincent", "not_home", last_changed=ago(minutes), friendly_name="Vincent")


def guard_for(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    return SecurityGuard(ctx, notifier, incidents), incidents


def no_security_calls(ctx):
    assert not [c for c in service_calls(ctx) if c[0] in ("alarm_control_panel", "lock", "cover")]


# --- tamper kinds from the real Ajax names ------------------------------------------------------

def test_tamper_kinds_of_ajax_and_nest(ctx):
    cases = {
        "binary_sensor.voordeur_behuizing_sabotage": ("Voordeur Behuizing sabotage", "tamper", "housing"),
        "binary_sensor.voordeur_kanteling": ("Voordeur Kanteling", "tamper", "tilt"),
        "binary_sensor.voordeur_vibratie": ("Voordeur Vibratie", "vibration", "vibration"),
        "binary_sensor.voordeur_extern_contact_verbroken": ("Voordeur Extern contact verbroken", "problem", "wire"),
        "binary_sensor.nest_protect_attic_removed_from_base": ("Melder Zolder Verwijderd van basis", "problem", "base"),
        "binary_sensor.inbraakalarm_deksel_geopend": ("Inbraakalarm Deksel geopend", "tamper", "housing"),
        "binary_sensor.radarr_out_of_memory": ("radarr Out of memory", "problem", None),
    }
    for eid, (name, cls, kind) in cases.items():
        assert tamper_kind(put(ctx, eid, "off", friendly_name=name, device_class=cls)) == kind, eid


async def test_housing_opened_while_armed_is_critical(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    signals = HouseSignals(ctx, notifier, incidents)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away")
    lid = put(ctx, "binary_sensor.raam_voorzijde_behuizing_sabotage", "on", area="Woonkamer", device="Raam voorzijde",
              friendly_name="Raam voorzijde Behuizing sabotage", device_class="tamper")
    await signals.on_state(lid)
    [incident] = incidents.active()
    assert incident["title"] == "Sabotage terwijl het alarm aan staat" and incident["severity"] == "critical"


async def test_tamper_while_disarmed_is_a_mutable_warning(ctx, notifier):
    from app.watcher.rules import apply_feedback

    incidents = IncidentManager(ctx.db, notifier)
    signals = HouseSignals(ctx, notifier, incidents)
    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed")
    tilt = put(ctx, "binary_sensor.dakraam_kanteling", "on", device="Dakraam", friendly_name="Dakraam Kanteling",
               device_class="tamper")
    await signals.on_state(tilt)
    assert ctx.db.events() == []  # a tilted window with the alarm off is normal
    wire = put(ctx, "binary_sensor.schuur_deur_extern_contact_verbroken", "on", device="Schuur deur",
               friendly_name="Schuur deur Extern contact verbroken", device_class="problem")
    await signals.on_state(wire)
    [event] = ctx.db.events()
    assert event["title"].endswith("Sabotagemelding") and "doorgeknipt" in event["message"]
    apply_feedback(ctx.db, event["id"], -1)
    signals._last.clear()
    await signals.on_state(wire)
    assert len(ctx.db.events()) == 1


# --- armed with something open ---------------------------------------------------------------------

async def test_arming_with_a_window_open_warns(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    panel = put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    await guard.on_state(panel, "arming", "armed_away")
    [incident] = incidents.active()
    assert incident["title"] == "Alarm aan met iets open"
    assert "Raam slaapkamer (Slaapkamer)" in ctx.db.events()[0]["message"]
    no_security_calls(ctx)


async def test_night_mode_ignores_windows_and_the_car(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "lock.fordpass_doorlock", "unlocked", friendly_name="Deuren", device="Ford Explorer")
    panel = put(ctx, "alarm_control_panel.inbraakalarm", "armed_night", friendly_name="Inbraakalarm")
    await guard.on_state(panel, "disarmed", "armed_night")
    assert incidents.active() == []
    await guard.on_state(panel, "armed_night", "armed_away")
    assert incidents.active() == []  # was on already


async def test_watcher_passes_alarm_changes_to_the_guard(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    watcher.guard, incidents = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    await watcher.handle_state_changed({
        "new_state": {"entity_id": "alarm_control_panel.inbraakalarm", "state": "armed_away"},
        "old_state": {"entity_id": "alarm_control_panel.inbraakalarm", "state": "arming"}})
    assert [i["title"] for i in incidents.active()] == ["Alarm aan met iets open"]


# --- alarm forgotten ---------------------------------------------------------------------------------

async def test_reminder_when_everybody_left_and_the_alarm_is_off(ctx, notifier):
    guard, _ = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed", friendly_name="Inbraakalarm")
    away(ctx, minutes=10)
    await guard.tick(NOW)
    assert ctx.db.events() == []  # just left
    away(ctx, minutes=25)
    await guard.tick(NOW)
    await guard.tick(NOW)
    assert titles(ctx).count("🛡️ Alarm staat uit") == 1
    assert "nooit zelf" in ctx.db.events()[0]["message"]
    no_security_calls(ctx)


async def test_no_reminder_when_armed_muted_or_turned_off(ctx, notifier):
    from app.watcher.guard import REMINDER_REF

    guard, _ = guard_for(ctx, notifier)
    away(ctx, minutes=60)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away")
    await guard.tick(NOW)
    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed")
    ctx.config.alarm_reminder_minutes = 0
    await guard.tick(NOW)
    ctx.config.alarm_reminder_minutes = 20
    ctx.db.mute(REMINDER_REF)
    await guard.tick(NOW)
    assert ctx.db.events() == []


async def test_bedtime_round_mentions_the_alarm(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed", friendly_name="Inbraakalarm")
    text = await watcher.bedtime_round()
    assert "🛡️ Alarm staat uit: Inbraakalarm" in text
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_night", friendly_name="Inbraakalarm")
    assert "Alarm" not in await watcher.bedtime_round()
    no_security_calls(ctx)


# --- cameras and the alarm's connection --------------------------------------------------------------

async def test_camera_offline_while_armed_once_per_outage(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away")
    put(ctx, "camera.achtertuin_clear", "idle", friendly_name="Achtertuin Clear")
    put(ctx, "camera.achtertuin_fluent", "idle", friendly_name="Achtertuin Fluent")
    put(ctx, "camera.mail_ups_camera", "unavailable", friendly_name="imap Mail UPS Camera", last_changed=ago(60))
    await guard.tick(NOW)
    put(ctx, "camera.achtertuin_clear", "unavailable", friendly_name="Achtertuin Clear", last_changed=ago(3))
    await guard.tick(NOW)
    assert incidents.active() == []  # one stream still works
    put(ctx, "camera.achtertuin_fluent", "unavailable", friendly_name="Achtertuin Fluent", last_changed=ago(3))
    await guard.tick(NOW)
    assert incidents.active() == []  # not long enough
    put(ctx, "camera.achtertuin_fluent", "unavailable", friendly_name="Achtertuin Fluent", last_changed=ago(12))
    put(ctx, "camera.achtertuin_clear", "unavailable", friendly_name="Achtertuin Clear", last_changed=ago(12))
    await guard.tick(NOW)
    await guard.tick(NOW)
    [incident] = incidents.active()
    assert incident["title"] == "Camera offline"
    message = ctx.db.events()[0]["message"]
    assert "Achtertuin" in message and "UPS" not in message and "alarm aan" in message


async def test_camera_offline_at_home_is_left_to_the_offline_rule(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "camera.achtertuin_clear", "idle", friendly_name="Achtertuin Clear")
    await guard.tick(NOW)
    put(ctx, "camera.achtertuin_clear", "unavailable", friendly_name="Achtertuin Clear", last_changed=ago(30))
    await guard.tick(NOW)
    assert incidents.active() == []


async def test_alarm_connection_lost(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", device_id="hub")
    link = put(ctx, "binary_sensor.inbraakalarm_verbinding_meldkamer", "on", device_id="hub", device="Inbraakalarm",
               friendly_name="Inbraakalarm Verbinding meldkamer", device_class="connectivity")
    put(ctx, "binary_sensor.woonkamer_aansluiting", "on", friendly_name="Woonkamer Aansluiting",
        device_class="connectivity")
    assert alarm_links(ctx.index) == [link]
    await guard.tick(NOW)
    put(ctx, "binary_sensor.inbraakalarm_verbinding_meldkamer", "off", device_id="hub", device="Inbraakalarm",
        friendly_name="Inbraakalarm Verbinding meldkamer", device_class="connectivity", last_changed=ago(6))
    await guard.tick(NOW)
    await guard.tick(NOW)
    [incident] = incidents.active()
    assert incident["title"] == "Alarmsysteem zonder verbinding" and incident["severity"] == "critical"


async def test_alarm_panel_unreachable_after_being_armed(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    await guard.tick(NOW)
    put(ctx, "alarm_control_panel.inbraakalarm", "unavailable", friendly_name="Inbraakalarm", last_changed=ago(8))
    await guard.tick(NOW)
    [incident] = incidents.active()
    assert incident["severity"] == "critical" and "onbereikbaar" in ctx.db.events()[0]["message"]


# --- one person, several cameras ---------------------------------------------------------------------

def cam(ctx, eid, name):
    return put(ctx, eid, "idle", friendly_name=name)


async def test_person_walking_to_the_back_while_away(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    away(ctx)
    front, back = cam(ctx, "camera.oprit", "Oprit"), cam(ctx, "camera.achtertuin", "Achtertuin")
    await guard.person_seen(front, "Oprit", "door")
    await guard.person_seen(front, "Oprit", "door")  # the same camera again is no route
    assert incidents.active() == []
    await guard.person_seen(back, "Achtertuin", "private", "Een man bij de schuur.")
    [incident] = incidents.active()
    assert incident["title"] == "Iemand loopt rond het huis" and incident["severity"] == "warning"
    assert "Oprit → Achtertuin" in ctx.db.events()[0]["message"]


async def test_two_front_cameras_are_no_route_but_armed_backyard_is_critical(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    away(ctx)
    await guard.person_seen(cam(ctx, "camera.deurbel", "Deurbel Voordeur"), "Voordeur", "door")
    await guard.person_seen(cam(ctx, "camera.oprit", "Oprit"), "Oprit", "door")
    assert incidents.active() == []  # the postman
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away")
    await guard.person_seen(cam(ctx, "camera.achtertuin", "Achtertuin"), "Achtertuin", "private")
    [incident] = incidents.active()
    assert incident["severity"] == "critical" and incident["kind"] == "security"


async def test_nobody_tracked_when_home(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    for eid, name, profile in (("camera.oprit", "Oprit", "door"), ("camera.achtertuin", "Achtertuin", "private")):
        await guard.person_seen(cam(ctx, eid, name), name, profile)
    assert incidents.active() == [] and guard._sightings == []


async def test_detection_feeds_the_route(ctx, notifier, monkeypatch):
    from app.watcher import rules

    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    watcher.guard, _ = guard_for(ctx, notifier)
    seen = []

    async def person_seen(camera, where, profile, description="", snapshot=None):
        seen.append((camera.entity_id, profile))

    watcher.guard.person_seen = person_seen
    results = iter([(SUSPICIOUS, "Iemand met een koevoet."), (VISITOR, "Een bezorger.")])

    async def classify(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(rules, "classify", classify)
    away(ctx)
    sensor = ctx.index.get("binary_sensor.voordeur_person")
    sensor.device_id = "cam1"
    for cam_ent in ("camera.voordeur_fluent", "camera.voordeur_clear"):
        ctx.index.get(cam_ent).device_id = "cam1"
    await watcher._detection(sensor)
    assert seen == [("camera.voordeur_clear", "door")]


# --- chat and weekly report --------------------------------------------------------------------------

def test_security_question_is_answered_from_the_sensors(ctx):
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    answer = security_answer(ctx, "Staat het alarm aan?")
    assert answer.startswith("Inbraakalarm: ingeschakeld (afwezig).")
    assert "Raam slaapkamer (Slaapkamer)" in answer
    assert security_answer(ctx, "Zet het alarm aan") is None
    assert security_answer(ctx, "Wanneer is het alarm afgegaan?") is None
    assert security_answer(ctx, "Hoe laat gaat mijn wekker?") is None


async def test_chat_shortcut_skips_the_model(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry

    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed", friendly_name="Inbraakalarm")
    result = await Agent(ctx, build_registry()).run("Is het huis beveiligd?", "c1")
    assert result.answer.startswith("Inbraakalarm: uitgeschakeld.")
    assert ctx.llm.requests == []
    assert "security_status" in build_registry().names


async def test_weekly_report(ctx, notifier):
    guard, incidents = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    await incidents.report("security", "Alarm afgegaan", "Inbraakalarm: het alarm is afgegaan!")
    await incidents.report("tamper", "Sabotagemelding", "Voordeur meldt sabotage.")
    put(ctx, "sensor.voordeur_batterij", "9", device="Voordeur", device_id="ajax1", device_class="battery",
        friendly_name="Voordeur Batterij")
    put(ctx, "binary_sensor.voordeur_behuizing_sabotage", "off", device="Voordeur", device_id="ajax1",
        device_class="tamper", friendly_name="Voordeur Behuizing sabotage")
    put(ctx, "sensor.telefoon_batterij", "5", device="Telefoon", device_id="phone", device_class="battery")
    text = await guard.send_weekly_report()
    assert "Alarm afgegaan: 1×" in text and "Sabotagemelding 1×" in text
    assert "Voordeur (9%)" in text and "Telefoon" not in text
    assert text.endswith("Alarm nu: Inbraakalarm: ingeschakeld (afwezig)")
    assert "🛡️ Beveiligingsrapport" in titles(ctx)


async def test_weekly_report_is_sent_on_sunday_evening(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    scheduler = Scheduler(ctx, notifier, watcher)
    scheduler.guard, _ = guard_for(ctx, notifier)
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away", friendly_name="Inbraakalarm")
    sent = []

    async def send():
        sent.append(True)
        return "ok"

    scheduler.guard.send_weekly_report = send
    notifier.now = lambda: datetime(2026, 10, 25, 19, 5)  # Sunday
    await scheduler.run_once()
    await scheduler.run_once()
    assert sent == [True]
