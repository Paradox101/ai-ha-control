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
    assert overview["open_doors_windows"] == ["binary_sensor.raam_slaapkamer (Raam slaapkamer, Slaapkamer) = on"]
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
