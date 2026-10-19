from app.config import Config
from app.policy import Decision, Policy


def test_area_from_device(index):
    assert index.get("light.woonkamer_plafond").area == "Woonkamer"


def test_search_by_name_and_area(index):
    results = index.search("lamp", area="woonkamer")
    assert [e.entity_id for e in results] == ["light.woonkamer_plafond"]


def test_search_alias_and_fuzzy(index):
    assert index.search("aanrecht")[0].entity_id == "light.keuken"
    assert index.search("temperatur woonkamer")[0].entity_id == "sensor.woonkamer_temperatuur"


def test_search_unknown_area(index):
    assert index.search("lamp", area="zolder") == []


def test_state_changed_updates_index(index):
    index.handle_state_changed({"entity_id": "light.keuken", "new_state": {**index.get("light.keuken").__dict__,
                                                                          "entity_id": "light.keuken",
                                                                          "state": "off", "attributes": {}}})
    assert index.get("light.keuken").state == "off"
    index.handle_state_changed({"entity_id": "light.keuken", "new_state": None})
    assert index.get("light.keuken") is None


def test_compact_and_overview(index):
    assert index.get("sensor.woonkamer_temperatuur").compact() == (
        "sensor.woonkamer_temperatuur (Temperatuur woonkamer, Woonkamer) = 21.4 °C"
    )
    overview = index.overview()
    assert overview["open_doors_windows"] == ["binary_sensor.raam_slaapkamer (Raam slaapkamer, Slaapkamer) = open"]
    assert len(overview["lights_on"]) == 1


def test_policy():
    policy = Policy(Config())
    assert policy.check("light", "turn_on", ["light.keuken"]).decision == Decision.ALLOW
    assert policy.check("lock", "unlock", ["lock.voordeur"]).decision == Decision.CONFIRM
    assert policy.check("homeassistant", "turn_on", ["lock.voordeur"]).decision == Decision.CONFIRM
    assert policy.check("cover", "open_cover", ["cover.garagedeur"]).decision == Decision.CONFIRM
    assert policy.check("shell_command", "run", []).decision == Decision.DENY
    assert policy.check("homeassistant", "restart", []).decision == Decision.DENY
    assert policy.check("light", "turn_off", []).decision == Decision.CONFIRM


def test_detection_sensor_tokens(index):
    index.update_state({"entity_id": "binary_sensor.carpet_cleaner", "state": "off", "attributes": {}})
    index.update_state({"entity_id": "binary_sensor.oprit_vehicle", "state": "off", "attributes": {}})
    assert not index.is_detection_sensor(index.get("binary_sensor.carpet_cleaner"))
    assert index.is_detection_sensor(index.get("binary_sensor.oprit_vehicle"))
    assert index.is_doorbell(index.get("binary_sensor.voordeur_visitor"))
    assert not index.is_doorbell(index.get("binary_sensor.voordeur_person"))


def test_supervisor_token_from_s6_environment(tmp_path, monkeypatch):
    import app.config as config_module

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr(config_module, "S6_ENV_DIR", tmp_path)
    monkeypatch.setenv("OPTIONS_FILE", str(tmp_path / "missing.json"))
    assert config_module.Config.load().ha_token == ""
    (tmp_path / "SUPERVISOR_TOKEN").write_text("abc123\n")
    assert config_module.Config.load().ha_token == "abc123"


def test_readable_states(index):
    index.update_state({"entity_id": "binary_sensor.achterdeur", "state": "off",
                        "attributes": {"friendly_name": "Achterdeur", "device_class": "door"}})
    index.update_state({"entity_id": "alarm_control_panel.huis", "state": "armed_away",
                        "attributes": {"friendly_name": "Alarm"}})
    assert index.get("binary_sensor.achterdeur").compact().endswith("= dicht")
    assert index.get("alarm_control_panel.huis").readable_state == "ingeschakeld (afwezig)"
    assert index.get("lock.voordeur").readable_state == "op slot"
    assert index.get("sensor.woonkamer_temperatuur").readable_state == "21.4 °C"


def test_person_only_home_zone_is_home(index):
    index.update_state({"entity_id": "person.nikky", "state": "Werk Nikky", "attributes": {"friendly_name": "Nikky"}})
    index.update_state({"entity_id": "person.vincent", "state": "not_home", "attributes": {"friendly_name": "Vincent"}})
    assert index.get("person.nikky").readable_state == "weg (in zone Werk Nikky)"
    assert index.get("person.vincent").readable_state == "weg"
    assert index.anyone_home() is False
    assert "person.nikky (Nikky) = weg (in zone Werk Nikky)" in index.overview()["people"]
    index.update_state({"entity_id": "person.vincent", "state": "home", "attributes": {"friendly_name": "Vincent"}})
    assert index.get("person.vincent").readable_state == "thuis"
    assert index.anyone_home() is True
