"""How the add-on reads a real, noisy house (entity export of 2026-10-19)."""

import time
from datetime import datetime, timedelta, timezone

from app.agent.autonomous import area_climate
from app.comfort import ComfortController, save_profile
from app.doctor import health_findings
from app.export import cleanup_rows, sheets
from app.house import arriving_zone, grid_power, is_price_unit, label, paused, ups_on_battery, waste_today_tomorrow
from app.tools.context import energy_info, price_sensor
from app.watcher.appliances import ApplianceMonitor
from app.watcher.baseline import Baseline, house_mode
from app.watcher.incidents import IncidentManager
from app.watcher.rules import Watcher
from app.watcher.signals import HouseSignals

from .test_security_comfort import act_everything, pushes, service_calls, titles


def put(ctx, entity_id, state, area=None, device=None, device_id=None, last_changed=None, **attrs):
    ctx.index.update_state({"entity_id": entity_id, "state": str(state), "attributes": attrs,
                            "last_changed": last_changed})
    ent = ctx.index.get(entity_id)
    ent.area, ent.device, ent.device_id = area, device, device_id
    if last_changed:
        ent.last_changed = last_changed
    return ent


def temp(ctx, entity_id, value, area, name, device=None, cls="temperature"):
    return put(ctx, entity_id, value, area=area, device=device, friendly_name=name, device_class=cls)


# --- rooms ------------------------------------------------------------------------------------

def test_room_temperature_ignores_fridge_setpoints_and_dew_point(ctx):
    temp(ctx, "sensor.achterdeur_temperatuur", 21, "Keuken", "Achterdeur temperatuur")
    temp(ctx, "sensor.shellyhtg3_54320456d0fc_temperature", 4.8, "Keuken", "Temperature", device="Koelkast sensor")
    temp(ctx, "sensor.vocht_sensor_keuken_temperature", 19.75, "Keuken", "Vocht sensor Keuken temperatuur")
    temp(ctx, "sensor.woonkamer_target", 5.0, "Woonkamer", "Woonkamer target")
    temp(ctx, "sensor.woonkamer_dew_point", 13.1, "Woonkamer", "Woonkamer dauwpunt")
    temp(ctx, "sensor.inverter_temperature", 32.1, "Zolder", "Inverter temperature", device="GoodWe")
    temp(ctx, "sensor.zolder_temperatuur", 20.2, "Zolder", "Zolder")
    air = area_climate(ctx.index)
    assert air["Keuken"]["temperature"] == 20.375  # median of 21 and 19.75, the fridge left out
    assert air["Woonkamer"]["temperature"] == 21.4  # only the real room sensor from the fixture
    assert air["Zolder"]["temperature"] == 20.2


async def test_profile_temperature_sensor_beats_the_room(ctx, notifier):
    await act_everything(ctx)
    temp(ctx, "sensor.babykamer", 15.0, "Woonkamer", "Thermometer bank")
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat", "targets": {"thuis": 20},
                          "temperature_sensor": "sensor.babykamer"})
    profile = ctx.db.profiles()[0]
    assert profile["temperature_sensor"] == "sensor.babykamer"
    comfort = ComfortController(ctx)
    assert comfort._room(profile, area_climate(ctx.index))["temperature"] == 15.0


async def test_pause_helper_keeps_comfort_off_the_thermostat(ctx, notifier):
    await act_everything(ctx)
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    put(ctx, "input_datetime.airco_gepauzeerd_tot", later, friendly_name="Airco gepauzeerd tot", has_date=True)
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat", "targets": {"thuis": 22},
                          "pause_entity": "input_datetime.airco_gepauzeerd_tot"})
    comfort = ComfortController(ctx)
    assert await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0)) == []
    assert service_calls(ctx) == [] and "gepauzeerd" in next(iter(comfort.paused.values()))
    put(ctx, "input_datetime.airco_gepauzeerd_tot", "2020-01-01 10:00:00", has_date=True)
    assert await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0))


def test_pause_helpers(ctx):
    put(ctx, "input_boolean.airco_correctie_uit", "on", friendly_name="Airco correctie uit")
    put(ctx, "timer.airco_timer", "active", friendly_name="Airco timer")
    put(ctx, "input_boolean.vrij", "off")
    assert paused(ctx.index, "input_boolean.airco_correctie_uit")
    assert paused(ctx.index, "timer.airco_timer")
    assert not paused(ctx.index, "input_boolean.vrij") and not paused(ctx.index, "")


async def test_conflicting_automations_are_listed(ctx, notifier):
    put(ctx, "automation.airco_koeling", "on", friendly_name="Airco koeling woonkamer", id="123")
    save_profile(ctx.db, {"area": "Woonkamer", "climate_entity": "climate.thermostaat", "targets": {"thuis": 22}})

    async def config(automation_id):
        return {"triggers": [{"entity_id": "sensor.woonkamer_temperatuur"}],
                "actions": [{"action": "climate.set_temperature", "target": {"entity_id": "climate.thermostaat"}}]}

    ctx.ha.get_automation_config = config
    assert await ComfortController(ctx).conflicts() == {"climate.thermostaat": ["Airco koeling woonkamer"]}


# --- ventilation and shading -----------------------------------------------------------------

async def test_no_window_advice_when_the_house_has_central_ventilation(ctx, notifier):
    await act_everything(ctx)
    temp(ctx, "sensor.co2_woonkamer", 1300, "Woonkamer", "CO2 woonkamer", cls="carbon_dioxide")
    put(ctx, "fan.itho_cve_fan", "on", area="Zolder", friendly_name="CVE Ventilator")
    save_profile(ctx.db, {"area": "Woonkamer"})
    await ComfortController(ctx).tick("thuis", datetime(2026, 10, 19, 12, 0))
    assert ctx.incidents.active() == []


async def test_central_ventilation_follows_all_rooms(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["switch=act"]
    ctx.config.ventilation_entity = "switch.wtw_hoog"
    put(ctx, "switch.wtw_hoog", "off", friendly_name="WTW hoog")
    temp(ctx, "sensor.co2_woonkamer", 1300, "Woonkamer", "CO2 woonkamer", cls="carbon_dioxide")
    temp(ctx, "sensor.co2_slaapkamer", 500, "Slaapkamer", "CO2 slaapkamer", cls="carbon_dioxide")
    save_profile(ctx.db, {"area": "Woonkamer"})
    save_profile(ctx.db, {"area": "Slaapkamer"})
    comfort = ComfortController(ctx)
    done = await comfort.tick("thuis", datetime(2026, 10, 19, 12, 0))
    assert "ventilatie aan" in done[-1] and ("switch", "turn_on") == service_calls(ctx)[-1][:2]
    put(ctx, "switch.wtw_hoog", "on")
    temp(ctx, "sensor.co2_woonkamer", 600, "Woonkamer", "CO2 woonkamer", cls="carbon_dioxide")
    done = await comfort.tick("thuis", datetime(2026, 10, 19, 12, 5))
    assert "ventilatie uit" in done[-1]


async def test_awning_stays_in_when_it_is_windy(ctx, notifier):
    await act_everything(ctx)
    ctx.config.autonomy = ["cover=act"]
    ctx.config.confirm_domains = []
    put(ctx, "sun.sun", "above_horizon", elevation=40)
    put(ctx, "weather.thuis", "sunny", temperature=27, wind_speed=40)
    put(ctx, "cover.zonnescherm", "closed", area="Woonkamer", friendly_name="Zonnescherm", device_class="awning")
    put(ctx, "cover.rolluik", "open", area="Woonkamer", friendly_name="Rolluik woonkamer", device_class="shutter")
    save_profile(ctx.db, {"area": "Woonkamer", "shade_covers": ["cover.zonnescherm", "cover.rolluik"]})
    ctx.index.services["cover"]["set_cover_position"] = {}
    await ComfortController(ctx).tick("thuis", datetime(2026, 10, 19, 12, 0))
    (call,) = [c for c in service_calls(ctx) if c[0] == "cover"]
    assert call[3]["entity_id"] == ["cover.rolluik"]


async def test_outdoor_sensor_beats_the_weather(ctx):
    ctx.config.outdoor_sensor = "sensor.buiten"
    temp(ctx, "sensor.buiten", -3, None, "Buiten")
    assert ComfortController(ctx)._outside() == -3


async def test_arriving_zone_preheats(ctx):
    put(ctx, "zone.voorloop_thuiskomst", "0", friendly_name="Voorloop thuiskomst")
    put(ctx, "person.vincent", "Voorloop thuiskomst", friendly_name="Vincent")
    assert "Voorloop thuiskomst" in arriving_zone(ctx.index)
    assert "zone" in ComfortController(ctx).arriving_soon(datetime(2026, 10, 19, 17, 0), 30)


# --- appliances ---------------------------------------------------------------------------------

def power(ctx, entity_id, watts):
    put(ctx, entity_id, watts, device="Wasmachine Energiemeter", device_id="wm", device_class="power",
        friendly_name=entity_id.split(".")[1].replace("_", " "), unit_of_measurement="W")


async def run_washer(ctx, monitor, t):
    for minute, watts in [(0, 0), (1, 2000), (40, 1), (60, 1)]:
        power(ctx, "sensor.wasmachine_energiemeter_vermogen", watts)
        power(ctx, "sensor.wasmachine_energiemeter_vermogen_fase_1", watts)
        await monitor.tick("thuis", t + minute * 60)


async def test_washer_with_a_phase_sensor_reports_once(ctx, notifier):
    await run_washer(ctx, ApplianceMonitor(ctx, notifier), time.time())
    assert [e["title"] for e in ctx.db.events()] == ["🧺 Wasmachine is klaar"]


async def test_quiet_appliance_has_its_own_automation(ctx, notifier):
    ctx.config.appliance_quiet = ["wasmachine"]
    await run_washer(ctx, ApplianceMonitor(ctx, notifier), time.time())
    assert ctx.db.events() == []


# --- cameras --------------------------------------------------------------------------------

def test_no_mail_or_map_cameras_and_babycam_is_off(ctx):
    put(ctx, "camera.imap_gmail_com_mail_amazon_delivery_camera", "idle", area="Meterkast",
        friendly_name="imap.gmail.com Mail Amazon Delivery Camera")
    put(ctx, "camera.pluisje_map", "idle", area="Woonkamer", friendly_name="Pluisje Huidige kaart")
    put(ctx, "camera.buienradar", "idle", friendly_name="Buienradar")
    baby = put(ctx, "camera.slaapkamer_voorkant_babycam", "idle", area="Slaapkamer voorkant", friendly_name="Babycam")
    cams = {c.entity_id for c in ctx.index.cameras()}
    assert "camera.pluisje_map" not in cams and "camera.buienradar" not in cams
    assert not any("imap" in c for c in cams) and "camera.slaapkamer_voorkant_babycam" in cams
    assert ctx.index.camera_profile(baby, []) == "off"
    assert ctx.index.camera_profile(baby, ["camera.slaapkamer_voorkant_babycam=private"]) == "private"
    sensor = put(ctx, "binary_sensor.pluisje_beweging", "on", area="Woonkamer", device_class="motion")
    put(ctx, "camera.pluisje_map", "idle", area="Woonkamer", device_id="vac")
    sensor.device_id = "vac"
    assert ctx.index.camera_for(sensor) is None or ctx.index.camera_for(sensor).entity_id != "camera.pluisje_map"


# --- house mode, presence, offline -----------------------------------------------------------

def test_own_mode_helper_decides_the_house_mode(ctx):
    now = datetime.now(timezone.utc)
    put(ctx, "input_text.actieve_scene", "slapen", friendly_name="Actieve Scene", last_changed=now.isoformat())
    assert house_mode(ctx.index, ctx.config, datetime(2026, 10, 19, 12, 0)) == "slapen"
    put(ctx, "input_text.actieve_scene", "afwezig", friendly_name="Actieve Scene",
        last_changed=(now - timedelta(hours=30)).isoformat())
    assert house_mode(ctx.index, ctx.config, datetime(2026, 10, 19, 12, 0)) == "thuis"  # Vincent is home: wins
    put(ctx, "person.vincent", "not_home", last_changed=(now - timedelta(hours=30)).isoformat())
    assert house_mode(ctx.index, ctx.config, datetime(2026, 10, 19, 12, 0)) == "vakantie"


def test_extra_presence_counts_as_home(ctx):
    put(ctx, "person.vincent", "not_home")
    put(ctx, "binary_sensor.home", "on", device_class="presence")
    assert ctx.index.anyone_home() is False
    ctx.index.config = ctx.config
    ctx.config.presence_extra = ["binary_sensor.home"]
    assert ctx.index.anyone_home() is True


def test_offline_only_for_the_house(ctx):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    baseline = Baseline(ctx.db, ctx.index, ctx.config)
    for eid, area in (("sensor.proxmox_cpu", "Server"), ("sensor.portainer_container", None),
                      ("sensor.koelkast", "Keuken"), ("camera.deurbel_poort", None)):
        put(ctx, eid, "1", area=area)
    baseline.offline()
    for eid, area in (("sensor.proxmox_cpu", "Server"), ("sensor.portainer_container", None),
                      ("sensor.koelkast", "Keuken"), ("camera.deurbel_poort", None)):
        put(ctx, eid, "unavailable", area=area, last_changed=old)
    assert {a.entity_id for a in baseline.offline()} == {"sensor.koelkast", "camera.deurbel_poort"}


# --- energy ---------------------------------------------------------------------------------

def test_energy_context_reads_this_house(ctx):
    put(ctx, "sensor.electricity_maps_co2_intensiteit", "200", unit_of_measurement="gCO2eq/kWh")
    assert not is_price_unit("gCO2eq/kWh") and is_price_unit("€/kWh") and is_price_unit("EUR/kWh")
    assert price_sensor(ctx) is None
    put(ctx, "sensor.energy_current_hour", "0.2", device="Solar production forecast", device_class="energy",
        friendly_name="Geschatte energieproductie - dit uur", unit_of_measurement="kWh")
    put(ctx, "sensor.energy_production_today_remaining", "0.3", device="Solar production forecast",
        device_class="energy", friendly_name="Geschatte energieproductie - resterend vandaag", unit_of_measurement="kWh")
    put(ctx, "sensor.p1_meter_power", "-1800", device="P1 meter", device_class="power", friendly_name="P1 meter Power")
    put(ctx, "sensor.p1_meter_power_phase_1", "-600", device="P1 meter", device_class="power")
    put(ctx, "sensor.pv_power", "2100", device="GoodWe", device_class="power", friendly_name="PV power")
    info = energy_info(ctx)
    assert info["solar_forecast"][0].startswith("Geschatte energieproductie - resterend")
    assert info["grid_now"].startswith("1800 W teruglevering") and info["solar_now"].startswith("2100 W")
    assert grid_power(ctx.index).entity_id == "sensor.p1_meter_power"


async def test_solar_surplus_tip_once_a_day(ctx, notifier, monkeypatch):
    put(ctx, "sensor.p1_meter_power", "-2000", device="P1 meter", device_class="power")
    power(ctx, "sensor.wasmachine_energiemeter_vermogen", 0)
    put(ctx, "sensor.fordpass_soc", "75", device="Ford Explorer", device_class="battery")
    signals = HouseSignals(ctx, notifier)
    t = time.time()
    monkeypatch.setattr("app.watcher.signals.time.time", lambda: t)
    await signals.tick(datetime(2026, 10, 19, 12, 0))
    t += 16 * 60
    await signals.tick(datetime(2026, 10, 19, 12, 16))
    await signals.tick(datetime(2026, 10, 19, 12, 30))
    (event,) = ctx.db.events()
    assert "2000 W terug" in event["message"] and "wasmachine" in event["message"] and "auto laden" in event["message"]


async def test_power_cut_from_the_ups(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    signals = HouseSignals(ctx, notifier, incidents)
    put(ctx, "sensor.eaton_statusgegevens", "OL", device="Eaton", device_id="ups")
    await signals.tick(datetime(2026, 10, 19, 22, 0))
    assert ups_on_battery(ctx.index) is None and ctx.db.events() == []
    put(ctx, "sensor.eaton_statusgegevens", "OB DISCHRG", device="Eaton", device_id="ups")
    put(ctx, "sensor.eaton_batterijduur", "1800", device="Eaton", device_id="ups", device_class="duration",
        unit_of_measurement="s", friendly_name="Eaton batterijduur")
    await signals.tick(datetime(2026, 10, 19, 22, 1))
    await signals.tick(datetime(2026, 10, 19, 22, 2))
    assert sum("Stroomstoring" in t for t in titles(ctx)) == 1
    assert "30 minuten" in ctx.db.events()[0]["message"]
    put(ctx, "sensor.eaton_statusgegevens", "OL", device="Eaton", device_id="ups")
    await signals.tick(datetime(2026, 10, 19, 22, 30))
    assert "Stroom is terug" in titles(ctx)[-1]


# --- security ---------------------------------------------------------------------------------

async def test_vibration_while_armed(ctx, notifier):
    incidents = IncidentManager(ctx.db, notifier)
    signals = HouseSignals(ctx, notifier, incidents)
    sensor = put(ctx, "binary_sensor.raam_voorzijde_vibratie", "on", area="Woonkamer", device="Raam voorzijde",
                 device_class="vibration")
    put(ctx, "alarm_control_panel.inbraakalarm", "disarmed")
    await signals.on_state(sensor)
    assert ctx.db.events() == []
    put(ctx, "alarm_control_panel.inbraakalarm", "armed_away")
    await signals.on_state(sensor)
    await signals.on_state(sensor)
    assert [i["title"] for i in incidents.active()] == ["Trilling of sabotage"]
    assert "Raam voorzijde (Woonkamer) meldt trilling" in ctx.db.events()[0]["message"]


async def test_external_contact_is_a_warning_while_disarmed(ctx, notifier):
    watcher = Watcher(ctx, notifier)
    watcher.incidents = IncidentManager(ctx.db, notifier)
    ent = put(ctx, "binary_sensor.achterdeur_alarm_extern_contact", "on", area="Keuken", device_class="safety",
              friendly_name="Achterdeur extern contact")
    await watcher._safety_alert(ent)
    (incident,) = watcher.incidents.active()
    assert incident["severity"] == "warning"


def test_car_lock_is_labelled(ctx):
    car = put(ctx, "lock.fordpass_wf0rpaef3ssm50705_doorlock", "unlocked", area="Achtertuin", friendly_name="Deuren",
              device="Ford Explorer")
    assert label(car) == "🚗 Deuren (auto)"


# --- summary, doctor, export -----------------------------------------------------------------

def test_waste_in_the_summary(ctx):
    put(ctx, "sensor.afvalwijzer_today", "geen")
    put(ctx, "sensor.afvalwijzer_tomorrow", "pmd")
    assert waste_today_tomorrow(ctx.index) == ["Morgen: pmd"]


async def test_doctor_suggests_settings_and_detector_tests(ctx, notifier):
    put(ctx, "valve.waterafsluiter", "open", friendly_name="Waterafsluiter")
    put(ctx, "tts.google_translate_nl_nl", "x")
    put(ctx, "binary_sensor.shellygas_gas", "off", device_id="gas", device_class="gas")
    put(ctx, "sensor.shellygas_self_test", "not_completed", device_id="gas", friendly_name="Shelly gas zelftest")
    put(ctx, "light.schuur", "off", friendly_name="Schuur")
    put(ctx, "light.schuur_2", "off", friendly_name="Schuur")
    findings = await health_findings(ctx)
    tips = findings["Instellingen die je kunt invullen"]
    assert "Hoofdkraan bij lekkage: valve.waterafsluiter" in tips
    assert "Spraakdienst: tts.google_translate_nl_nl" in tips
    assert findings["Rook-, gas- en CO-melders"] == ["Shelly gas zelftest: zelftest nog niet gedaan"]
    assert findings["Dubbele entiteiten (oude kopie met _2/_3, volledige lijst in de export)"] == ["light.schuur_2"]


def test_export_has_a_cleanup_sheet(ctx):
    put(ctx, "light.schuur", "off", friendly_name="Schuur")
    put(ctx, "light.schuur_2", "off", friendly_name="Schuur")
    put(ctx, "sensor.oud", "unavailable", last_changed=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    rows = cleanup_rows(ctx.index)
    assert rows[0][:3] == ["light.schuur_2", "Schuur", "kopie van light.schuur"]
    assert rows[1][0] == "sensor.oud" and "40 dagen" in rows[1][2]
    assert [name for name, _, _ in sheets(ctx.index)][-1] == "Opruimen"
    assert pushes(ctx) == []


# --- chat: temperatures per room ---------------------------------------------------------------

async def test_chat_gets_every_room_temperature_and_skips_rooms_without_sensor(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry

    temp(ctx, "sensor.badkamer_temperatuur", 21.34, "Badkamer", "Badkamer temperatuur")
    temp(ctx, "sensor.badkamer_vocht", 71, "Badkamer", "Badkamer vocht", cls="humidity")
    temp(ctx, "sensor.zolder_temperatuur", 17.2, "Zolder", "Zolder temperatuur")
    temp(ctx, "sensor.goodwe_inverter_temperature", 32.1, "Zolder", "Inverter temperature")
    put(ctx, "climate.slaapkamer_achterkant", "cool", area="Slaapkamer achterkant", friendly_name="Airco",
        current_temperature=20.1, temperature=19)
    ctx.index.areas["schuur"] = "Schuur"
    prompt = Agent(ctx, build_registry()).system_prompt()
    line = next(x for x in prompt.splitlines() if x.startswith("- Temperatuur per ruimte"))
    assert "Badkamer 21,3 °C (RV 71 %)" in line and "Zolder 17,2 °C" in line
    assert "Slaapkamer achterkant 20,1 °C" in line and "Schuur" not in line and "32" not in line
    overview = await build_registry().run(ctx, "get_home_overview", {})
    assert '"Zolder": {"temperature": 17.2}' in overview


async def test_check_intro_sentences_are_stripped(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry

    from .conftest import FakeLLM

    ctx.llm = FakeLLM([
        {"role": "assistant", "content": "Woonkamer 19,7 °C."},
        {"role": "assistant", "content": "De momentopname bevat al de benodigde informatie. Hier is het antwoord "
                                         "nog een keer:\n\n- Woonkamer: 19,7 °C\n- Badkamer: 21,3 °C"},
    ])
    result = await Agent(ctx, build_registry()).run("Graag alle temperaturen", "q9")
    assert result.answer == "- Woonkamer: 19,7 °C\n- Badkamer: 21,3 °C"


# --- car ----------------------------------------------------------------------------------------

FORD = "Ford Explorer (WF0RPAEF3SSM50705)"


def ford(ctx, charging="NOT_PLUGGED_IN", plug="DISCONNECTED", charger_w=0):
    p = "fordpass_wf0rpaef3ssm50705"
    put(ctx, f"sensor.{p}_soc", 75.0, "Achtertuin", FORD, friendly_name="Laadtoestand", device_class="battery",
        unit_of_measurement="%")
    put(ctx, f"sensor.{p}_elveh", 226.0, "Achtertuin", FORD, friendly_name="EV bereik", device_class="distance",
        unit_of_measurement="km")
    put(ctx, f"sensor.{p}_odometer", 38757.0, "Achtertuin", FORD, friendly_name="Kilometerteller",
        device_class="distance", unit_of_measurement="km")
    put(ctx, f"sensor.{p}_elvehcharging", charging, "Achtertuin", FORD, friendly_name="EV oplaadstatus")
    put(ctx, f"sensor.{p}_elvehplug", plug, "Achtertuin", FORD, friendly_name="EV stekker")
    put(ctx, f"lock.{p}_doorlock", "locked", "Achtertuin", FORD, friendly_name="Deuren")
    # A 12 V battery and a phone that are "full" must never answer "how full is the car".
    put(ctx, f"sensor.{p}_battery", 100, "Achtertuin", FORD, friendly_name="12V accu", device_class="battery",
        unit_of_measurement="%")
    put(ctx, "binary_sensor.telefoon_battery", "off", device="Telefoon", friendly_name="Telefoon batterij",
        device_class="battery")
    put(ctx, "sensor.laadpaal_active_power_total_socket_1", charger_w, "Achtertuin", "Laadpaal",
        friendly_name="Laadpaal Active Power Total Socket 1", device_class="power", unit_of_measurement="W")
    put(ctx, "sensor.laadpaal_car_pwm_duty_cycle_socket_1", 26.6, "Achtertuin", "Laadpaal",
        friendly_name="Laadpaal Car PWM Duty Cycle Socket 1", device_class="power_factor", unit_of_measurement="%")


def test_car_status_uses_the_ev_state_of_charge(ctx):
    from app.house import car_status
    ford(ctx)
    assert car_status(ctx.index) == [f"{FORD}: laadtoestand (EV-accu) 75 %, laadt niet, stekker eruit, "
                                     "bereik 226 km, op slot"]
    ford(ctx, charging="CHARGING", plug="CONNECTED")
    assert "NU AAN HET LADEN" in car_status(ctx.index)[0]
    # FordPass lags behind; the charge point's power is live.
    ford(ctx, charger_w=7400)
    line = car_status(ctx.index)[0]
    assert "75 %" in line and "NU AAN HET LADEN (laadpaal 7,4 kW)" in line and "100" not in line


async def test_car_question_reads_the_car_line(ctx):
    from app.agent.loop import Agent
    from app.tools.home import build_registry, get_home_overview
    ford(ctx, charger_w=7400)
    prompt = Agent(ctx, build_registry()).system_prompt()
    assert f"- Auto: {FORD}: laadtoestand (EV-accu) 75 %, NU AAN HET LADEN" in prompt
    assert "75 %" in (await get_home_overview(ctx))["car"][0]
    assert ctx.index.search("auto accu")[0].entity_id.startswith("sensor.fordpass")
