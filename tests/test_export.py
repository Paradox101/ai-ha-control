"""Excel/text export of the entities: odd data from a real house must not break it."""

import io
import zipfile

from app.export import entities_text, entities_xlsx
from app.ha.index import EntityIndex
from tests.conftest import AREAS, DEVICES, ENTITIES, SERVICES, STATES


def house_with_odd_entities() -> EntityIndex:
    index = EntityIndex()
    states = STATES + [
        {"entity_id": "light.hal", "state": "on", "attributes": {"friendly_name": "Hal", "brightness": None}},
        {"entity_id": "sensor.leeg", "state": None, "attributes": {"friendly_name": None, "unit_of_measurement": None}},
    ]
    # Newer Home Assistant versions put None in the alias list for "the entity's own name".
    entities = ENTITIES + [{"entity_id": "light.hal", "aliases": [None, "gang"]}]
    index.load(states, AREAS, DEVICES, entities, SERVICES)
    return index


def test_alias_none_is_ignored():
    index = house_with_odd_entities()
    assert index.get("light.hal").aliases == ["gang"]
    assert index.search("gang")[0].entity_id == "light.hal"


def test_export_with_odd_entities():
    index = house_with_odd_entities()
    text = entities_text(index)
    assert "light.hal\tHal\tlight" in text and "\tgang\t" in text
    with zipfile.ZipFile(io.BytesIO(entities_xlsx(index))) as xlsx:
        assert "sensor.leeg" in xlsx.read("xl/worksheets/sheet1.xml").decode()


def test_broken_entity_is_skipped_not_fatal():
    index = house_with_odd_entities()
    index.get("light.hal").aliases = [object()]  # anything str() can handle still works
    broken = index.get("light.keuken")
    broken.useful_attributes = lambda: 1 / 0
    text = entities_text(index)
    assert "light.keuken\t(fout bij exporteren" in text and "light.woonkamer_plafond" in text
