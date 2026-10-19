"""Every rule against Vincent's real house (3657 entities). Skipped without the fixture (see house.py)."""

from datetime import datetime

import pytest

from app.agent.autonomous import area_climate
from app.house import car_status, duplicates, is_car, rechargeable_devices
from app.tools.history import detection_places
from app.watcher.baseline import Baseline

from .house import fixture_path, load_index

pytestmark = pytest.mark.skipif(fixture_path() is None, reason="no house fixture")


@pytest.fixture(scope="module")
def index():
    return load_index()


def test_rooms_are_rooms(index):
    air = area_climate(index)
    assert "Server" not in air
    for area, values in air.items():
        if "temperature" in values:
            assert 10 < values["temperature"] < 30, area


def test_detections_are_cameras_and_motion_detectors(index):
    for place in detection_places(index).values():
        for sensor in place.sensors:
            assert sensor.entity_id != "binary_sensor.home"
            assert not sensor.entity_id.endswith("_openen")


def test_only_real_doors_and_windows(index):
    for ent in index.entities.values():
        if index.is_opening(ent):
            assert not ent.entity_id.endswith(("_openen", "_predicted")), ent.entity_id


def test_the_car_is_the_ford(index):
    lines = car_status(index)
    assert len(lines) == 1 and lines[0].startswith("Ford Explorer")
    assert not any(is_car(e) for e in index.entities.values() if e.domain in ("camera", "binary_sensor", "vacuum"))
    assert "ford_explorer_wf0rpaef3ssm50705_achtertuin" in rechargeable_devices(index)


def test_no_server_anomalies(index, tmp_path):
    from app.config import Config
    from app.storage.db import Database

    baseline = Baseline(Database(str(tmp_path)), index, Config(data_dir=str(tmp_path)))
    assert not any(baseline.learnable(e) for e in index.entities.values() if e.area == "Server")


def test_no_false_duplicates(index):
    found = {d.entity_id for _, d in duplicates(index)}
    assert "sensor.sirene_batterij_2" not in found and "camera.schuur_vloeiend_2" in found


def test_overview_and_prompt(index, tmp_path):
    from tests.conftest import FakeHA, FakeLLM
    from app.agent.loop import Agent
    from app.config import Config
    from app.policy import Policy
    from app.storage.db import Database
    from app.tools.actions import ConfirmationStore
    from app.tools.base import ToolContext
    from app.tools.home import build_registry

    config = Config(data_dir=str(tmp_path))
    ctx = ToolContext(config=config, ha=FakeHA(), index=index, llm=FakeLLM(), policy=Policy(config, index),
                      db=Database(str(tmp_path)), confirmations=ConfirmationStore(), conversation_id="t",
                      timezone="Europe/Amsterdam")
    prompt = Agent(ctx, build_registry()).snapshot(datetime(2026, 10, 19, 16, 30))
    assert "Server" not in prompt and "Slot: Deuren" not in prompt and "Pluisje" not in prompt
    assert "Deuren/ramen" in prompt and "alles dicht" in prompt
