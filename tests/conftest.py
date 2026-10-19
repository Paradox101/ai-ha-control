from __future__ import annotations

from typing import Any

import pytest

from app.config import Config
from app.ha.index import EntityIndex
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
]
AREAS = [{"area_id": "woonkamer", "name": "Woonkamer"}, {"area_id": "slaapkamer", "name": "Slaapkamer"},
         {"area_id": "keuken", "name": "Keuken"}]
DEVICES = [{"id": "dev1", "name": "Hue plafond", "area_id": "woonkamer"}]
ENTITIES = [
    {"entity_id": "light.woonkamer_plafond", "device_id": "dev1"},
    {"entity_id": "sensor.woonkamer_temperatuur", "area_id": "woonkamer"},
    {"entity_id": "binary_sensor.raam_slaapkamer", "area_id": "slaapkamer"},
    {"entity_id": "light.keuken", "area_id": "keuken", "aliases": ["aanrecht"]},
]
SERVICES = {
    "light": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    "switch": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    "climate": {"set_temperature": {}, "set_hvac_mode": {}, "set_preset_mode": {}},
    "lock": {"lock": {}, "unlock": {}},
    "homeassistant": {"restart": {}, "turn_on": {}},
    "shell_command": {"run": {}},
}


class FakeHA:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def call_service(self, domain, service, data=None, target=None):
        self.calls.append((domain, service, data, target))

    async def history(self, entity_ids, start_time, end_time=None):
        return {entity_ids[0]: [{"s": "20.0", "lu": 1.0}, {"s": "22.0", "lu": 2.0}, {"s": "21.0", "lu": 3.0}]}

    async def camera_snapshot(self, entity_id):
        return b"jpeg"


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, model=None, timeout=300):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        return self.responses.pop(0) if self.responses else {"role": "assistant", "content": "klaar"}

    async def describe_image(self, image, question):
        return "Er staat een pakketje voor de deur."


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
        policy=Policy(config),
        db=Database(config.data_dir),
        confirmations=ConfirmationStore(),
        conversation_id="test",
        timezone="Europe/Amsterdam",
    )
