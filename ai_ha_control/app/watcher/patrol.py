"""Camera patrol: compare snapshots cheaply, only ask the vision model when the picture changed."""

from __future__ import annotations

import io
import logging
import time
from typing import TYPE_CHECKING

from ..llm.ollama import PRIORITY_PATROL
from .baseline import MODE_AWAY, MODE_SLEEP, MODE_VACATION
from .rules import quote
from .vision import QUIET_CATEGORIES, SUSPICIOUS, classify

if TYPE_CHECKING:
    from ..tools.base import ToolContext
    from .incidents import IncidentManager, SnapshotStore

_LOGGER = logging.getLogger(__name__)

SIGNATURE_SIZE = (32, 18)
CHANGE_THRESHOLD = 12.0   # mean absolute difference (0-255) after removing overall brightness


def signature(image: bytes) -> list[int]:
    """Tiny grayscale thumbnail with the average brightness removed (robust to clouds/dusk)."""
    from PIL import Image  # imported lazily: only needed when patrolling

    with Image.open(io.BytesIO(image)) as img:
        pixels = list(img.convert("L").resize(SIGNATURE_SIZE).tobytes())
    mean = sum(pixels) / len(pixels)
    return [round(p - mean) for p in pixels]


def difference(a: list[int], b: list[int]) -> float:
    if len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


class Patrol:
    def __init__(self, ctx: ToolContext, incidents: IncidentManager, snapshots: SnapshotStore) -> None:
        self._ctx = ctx
        self._incidents = incidents
        self._snapshots = snapshots
        self._previous: dict[str, list[int]] = {}
        self._last_run = -1e9
        self.on_observation = None  # callback(text) to inform the autonomous agent

    def active(self, mode: str) -> bool:
        setting = self._ctx.config.camera_patrol
        if setting == "always":
            return True
        return setting == "away" and mode in (MODE_AWAY, MODE_VACATION, MODE_SLEEP)

    def due(self, mode: str) -> bool:
        if not self.active(mode):
            self._previous.clear()  # start fresh next time, the scene will have changed
            return False
        return time.monotonic() - self._last_run >= self._ctx.config.patrol_minutes * 60

    async def run(self) -> int:
        """Check every camera once; returns the number of cameras where something was seen."""
        self._last_run = time.monotonic()
        seen = 0
        for cam in self._ctx.index.cameras():
            profile = self._ctx.index.camera_profile(cam, self._ctx.config.camera_profiles)
            if profile == "off":
                continue
            try:
                image = await self._ctx.ha.camera_snapshot(cam.entity_id)
                sig = signature(image)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Patrol snapshot of %s failed: %s", cam.entity_id, err)
                continue
            previous = self._previous.get(cam.entity_id)
            self._previous[cam.entity_id] = sig
            if previous is None or difference(previous, sig) < CHANGE_THRESHOLD:
                continue
            try:
                category, description = await classify(
                    self._ctx.llm, image, cam.name, "Dit is een controleronde; het beeld is veranderd.",
                    priority=PRIORITY_PATROL)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Patrol analysis of %s failed: %s", cam.entity_id, err)
                continue
            if category in QUIET_CATEGORIES or (profile == "street" and category != SUSPICIOUS):
                continue
            seen += 1
            snapshot = self._snapshots.save(cam.entity_id, image)
            text = f"{self._ctx.index.camera_label(cam)}: {description}"
            await self._incidents.report(
                "presence", "Activiteit gezien tijdens patrouille", text,
                severity="warning" if category == SUSPICIOUS else "info",
                entity_id=cam.entity_id, snapshot=snapshot, image_entity=cam.entity_id,
            )
            if self.on_observation:
                self.on_observation(f"Patrouille: {cam.name}: {quote(description)}")
        return seen
