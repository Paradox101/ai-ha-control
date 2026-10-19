from __future__ import annotations

from typing import Any

import pytest

from app.config import Config
from app.ha.index import EntityIndex
from app.notify import Notifier
from app.policy import Policy
from app.storage.db import Database
from app.tools.actions import ConfirmationStore
from app.tools.base import ToolContext

STATES = [
    {"entity_id": "light.woonkamer_plafond", "state": "off",
     "attributes": {"friendly_name": "Plafondlamp woonkamer"}},
    {"entity_id": "light.keuken", "state": "on", "attributes": {"friendly_name": "Keukenlamp", "brightness": 128}},
    {"entity_id": "sensor.woonkamer_temperatuur", "state": "21.4",
     "attributes": {"friendly_name": "Temperatuur woonkamer", "device_class": "temperature",
                    "unit_of_measurement": "°C"}},
    {"entity_id": "binary_sensor.raam_slaapkamer", "state": "on",
     "attributes": {"friendly_name": "Raam slaapkamer", "device_class": "window"}},
    {"entity_id": "climate.thermostaat", "state": "heat",
     "attributes": {"friendly_name": "Thermostaat", "current_temperature": 20.5, "temperature": 20,
                    "hvac_modes": ["off", "heat"], "min_temp": 5, "max_temp": 30}},
    {"entity_id": "lock.voordeur", "state": "locked", "attributes": {"friendly_name": "Voordeur slot"}},
    {"entity_id": "camera.voordeur_fluent", "state": "idle", "attributes": {"friendly_name": "Voordeur"}},
    {"entity_id": "switch.koffiezetapparaat", "state": "off", "attributes": {"friendly_name": "Koffiezetapparaat"}},
    {"entity_id": "person.vincent", "state": "home", "attributes": {"friendly_name": "Vincent"}},
    {"entity_id": "camera.voordeur_clear", "state": "idle", "attributes": {"friendly_name": "Voordeur Clear"}},
    {"entity_id": "binary_sensor.voordeur_person", "state": "off", "attributes": {"friendly_name": "Voordeur Persoon"}},
    {"entity_id": "binary_sensor.voordeur_visitor", "state": "off", "attributes": {"friendly_name": "Voordeur Bezoeker"}},
    {"entity_id": "binary_sensor.rookmelder_zolder", "state": "off",
     "attributes": {"friendly_name": "Rookmelder zolder", "device_class": "smoke"}},
    {"entity_id": "weather.thuis", "state": "cloudy", "attributes": {"friendly_name": "Thuis", "temperature": 12}},
    {"entity_id": "sensor.stroom_totaal", "state": "1203.5",
     "attributes": {"friendly_name": "Stroomverbruik totaal", "device_class": "energy",
                    "state_class": "total_increasing", "unit_of_measurement": "kWh"}},
    {"entity_id": "cover.garagedeur", "state": "closed",
     "attributes": {"friendly_name": "Garagedeur", "device_class": "garage"}},
]
AREAS = [{"area_id": "woonkamer", "name": "Woonkamer"}, {"area_id": "slaapkamer", "name": "Slaapkamer"},
         {"area_id": "keuken", "name": "Keuken"}]
DEVICES = [{"id": "dev1", "name": "Hue plafond", "area_id": "woonkamer"},
           {"id": "cam1", "name": "Voordeur", "area_id": None}]
ENTITIES = [
    {"entity_id": "light.woonkamer_plafond", "device_id": "dev1"},
    {"entity_id": "sensor.woonkamer_temperatuur", "area_id": "woonkamer"},
    {"entity_id": "binary_sensor.raam_slaapkamer", "area_id": "slaapkamer"},
    {"entity_id": "light.keuken", "area_id": "keuken", "aliases": ["aanrecht"]},
    {"entity_id": "camera.voordeur_fluent", "device_id": "cam1"},
    {"entity_id": "camera.voordeur_clear", "device_id": "cam1"},
    {"entity_id": "binary_sensor.voordeur_person", "device_id": "cam1"},
    {"entity_id": "binary_sensor.voordeur_visitor", "device_id": "cam1"},
]
SERVICES = {
    "light": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    "switch": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    "climate": {"set_temperature": {}, "set_hvac_mode": {}, "set_preset_mode": {}},
    "lock": {"lock": {}, "unlock": {}},
    "cover": {"open_cover": {}, "close_cover": {}},
    "notify": {"mobile_app_telefoon": {}},
    "persistent_notification": {"create": {}},
    "homeassistant": {"restart": {}, "turn_on": {}},
    "shell_command": {"run": {}},
}


class FakeHA:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.saved_automations: dict[str, dict] = {}
        self.logbook_entries: list[dict] = []
        self.validation: dict = {"triggers": {"valid": True, "error": None}, "actions": {"valid": True, "error": None}}
        self.connected = True
        self.states_set: dict = {}
        self.created_booleans: list = []

    async def call_service(self, domain, service, data=None, target=None, return_response=False):
        self.calls.append((domain, service, data, target))
        if return_response and (domain, service) == ("weather", "get_forecasts"):
            return {"weather.thuis": {"forecast": [
                {"datetime": "2026-10-20T10:00:00+00:00", "condition": "rainy", "temperature": 14, "templow": 8,
                 "precipitation": 4.2, "precipitation_probability": 90},
            ]}}

    async def validate_config(self, triggers, conditions, actions):
        return self.validation

    async def save_automation_config(self, automation_id, config):
        self.saved_automations[automation_id] = config
        return {"result": "ok"}

    async def related(self, item_type, item_id):
        return {"automation": ["automation.vertrek"]} if item_id == "light.keuken" else {}

    async def set_state(self, entity_id, state, attributes):
        self.states_set[entity_id] = (state, attributes)

    async def create_input_boolean(self, name, icon="mdi:robot"):
        self.created_booleans.append(name)

    async def logbook(self, entity_ids, start_time, end_time):
        return [e for e in self.logbook_entries if e["entity_id"] in entity_ids]

    async def history(self, entity_ids, start_time, end_time=None):
        return {entity_ids[0]: [{"s": "20.0", "lu": 1.0}, {"s": "22.0", "lu": 2.0}, {"s": "21.0", "lu": 3.0}]}

    async def camera_snapshot(self, entity_id):
        return b"jpeg"


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, model=None, timeout=300, priority=1, on_delta=None, schema=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model,
                              "streamed": on_delta is not None, "schema": schema})
        response = self.responses.pop(0) if self.responses else {"role": "assistant", "content": "klaar"}
        if on_delta is not None and response.get("content") and not response.get("tool_calls"):
            for word in response["content"].split(" "):
                await on_delta(word + " ")
        return response

    async def describe_image(self, image, question, priority=1, schema=None):
        return "Er staat een pakketje voor de deur."


@pytest.fixture(autouse=True)
async def no_background_verification(monkeypatch):
    """Autonomous actions are verified ~45 s later; in tests that check is run explicitly."""
    from app.tools import actions

    monkeypatch.setattr(actions, "VERIFY_DELAY", 3600.0)
    yield
    for task in list(actions._background):
        task.cancel()


@pytest.fixture
def index() -> EntityIndex:
    idx = EntityIndex()
    idx.load(STATES, AREAS, DEVICES, ENTITIES, SERVICES)
    return idx


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(dry_run=False, data_dir=str(tmp_path))


@pytest.fixture
def ctx(config, index) -> ToolContext:
    return ToolContext(
        config=config,
        ha=FakeHA(),
        index=index,
        llm=FakeLLM(),
        policy=Policy(config, index),
        db=Database(config.data_dir),
        confirmations=ConfirmationStore(),
        conversation_id="test",
        timezone="Europe/Amsterdam",
    )


@pytest.fixture
def notifier(ctx) -> Notifier:
    ctx.config.quiet_hours = ""
    n = Notifier(ctx.config, ctx.ha, ctx.db)
    n.timezone = "Europe/Amsterdam"
    ctx.notifier = n
    return n
