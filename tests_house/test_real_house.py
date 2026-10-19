"""Every rule against Vincent's real house (3657 entities). Skipped without the fixture (see house.py)."""

from datetime import datetime

import pytest

from app.agent.autonomous import area_climate
from app.house import car_answer, car_status, duplicates, is_car, rechargeable_devices
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
    assert car_answer(index, "Hoe vol is de auto").startswith("De accu is 75 % vol")
    assert car_answer(index, "Is de auto op slot en hoe vol is de accu?").startswith("De auto staat op slot. De accu")
    assert car_answer(index, "staat de stofzuiger op auto?") is None


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


def test_status_questions_from_the_house(index):
    from app.answers import status_answer
    from app.config import Config

    config = Config()
    temps = status_answer(index, config, "Graag alle temperaturen")
    assert "Woonkamer 20 °C" in temps and "Meterkast" not in temps and "Server" not in temps
    assert status_answer(index, config, "Wat is de temperatuur in de slaapkamer?").startswith(
        "Slaapkamer achterkant 21,5 °C")
    assert status_answer(index, config, "Hoe warm is het buiten?") == "Buiten is het 14,4 °C."
    assert status_answer(index, config, "Is Nikky thuis?") == "Nikky van Essen is in Werk Nikky."
    assert status_answer(index, config, "Welke lampen staan aan?") == "Er staan geen lampen aan."
    assert status_answer(index, config, "Staan er ramen open?") == "Alle ramen en deuren met een sensor zijn dicht."
    assert status_answer(index, config, "Hoe vol is de auto?").startswith("De accu is 75 % vol")
    assert status_answer(index, config, "staat de hoeklamp aan?") == "Hoeklamp: uit."
    # Four entities called "TV Woonkamer": the media player speaks for them.
    assert status_answer(index, config, "staat de tv aan?") == "TV Woonkamer: aan."
    assert status_answer(index, config, "staat de airco aan?") == (
        "Slaapkamer Airco (Slaapkamer achterkant): uit, nu 20 °C; Woonkamer Airco: uit, nu 19,9 °C.")
