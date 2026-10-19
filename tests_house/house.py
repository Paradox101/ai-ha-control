"""Vincent's real house (entity export of 2026-10-19) loaded into the index, for checks against real entities.

The JSON is made with scripts/export_to_fixture.py and is not in the repository (it is a real house). Point
HOUSE_FIXTURE at it; tests_house/ is skipped without it.
"""

import json
import os
from pathlib import Path

from app.ha.index import EntityIndex
from tests.conftest import SERVICES

DEFAULT = Path("/mnt/project-files/testdata/huis-2026-10-19.json")


def fixture_path() -> Path | None:
    path = Path(os.environ.get("HOUSE_FIXTURE") or DEFAULT)
    return path if path.exists() else None


def load_index() -> EntityIndex:
    data = json.loads(fixture_path().read_text(encoding="utf-8"))
    index = EntityIndex()
    index.load(data["states"], data["areas"], data["devices"], data["entities"], SERVICES)
    return index
