"""Incidents: related observations grouped into one story with a timeline, instead of loose notifications."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..notify import Notifier
    from ..storage.db import Database

_LOGGER = logging.getLogger(__name__)

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
SEVERITY_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
SNAPSHOT_NAME = re.compile(r"^[0-9]+_[a-z0-9_.]+\.jpg$")
AUTO_RESOLVE_HOURS = 24


class SnapshotStore:
    """JPEG snapshots kept for incident timelines."""

    def __init__(self, data_dir: str) -> None:
        self.directory = Path(data_dir) / "snapshots"
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, camera_id: str, image: bytes) -> str:
        name = f"{int(time.time() * 1000)}_{camera_id.replace('camera.', '')}.jpg"
        (self.directory / name).write_bytes(image)
        return name

    def path(self, name: str) -> Path | None:
        if not SNAPSHOT_NAME.match(name):
            return None
        path = self.directory / name
        return path if path.is_file() else None

    def cleanup(self, days: int) -> int:
        limit = time.time() - days * 86400
        removed = 0
        for file in self.directory.glob("*.jpg"):
            if file.stat().st_mtime < limit:
                file.unlink(missing_ok=True)
                removed += 1
        return removed


class IncidentManager:
    def __init__(self, db: Database, notifier: Notifier) -> None:
        self._db = db
        self._notifier = notifier

    def active(self) -> list[dict[str, Any]]:
        return self._db.incidents(status="active")

    def find(self, kind: str | None = None, title: str | None = None, within: float | None = None) -> dict | None:
        now = time.time()
        for incident in self.active():
            if kind and incident["kind"] != kind:
                continue
            if title and incident["title"].lower() != title.lower():
                continue
            if within is not None and now - incident["updated"] > within:
                continue
            return incident
        return None

    async def report(
        self,
        kind: str,
        title: str,
        text: str,
        severity: str = "warning",
        entity_id: str | None = None,
        snapshot: str | None = None,
        image_entity: str | None = None,
        correlate_seconds: float = 600,
        push: bool = True,
        critical: bool = False,
        announce: str | None = None,
        respect_quiet_hours: bool = True,
        ref: str | None = None,
    ) -> int:
        """Add to a recent incident of the same kind, or open a new one and notify."""
        severity = severity if severity in SEVERITY_ORDER else "warning"
        existing = self.find(kind=kind, within=correlate_seconds)
        if existing:
            incident_id = existing["id"]
            self._db.add_incident_event(incident_id, text, entity_id, snapshot)
            escalated = SEVERITY_ORDER[severity] > SEVERITY_ORDER.get(existing["severity"], 0)
            if escalated:
                self._db.update_incident(incident_id, severity=severity)
            if not (escalated and push):
                return incident_id  # same story continues: timeline only, no new push
        else:
            incident_id = self._db.open_incident(kind, severity, title, text)
            self._db.add_incident_event(incident_id, text, entity_id, snapshot)
            if not push:
                return incident_id
        await self._notifier.send(
            f"{SEVERITY_ICON[severity]} {title}", text, kind=kind.split(":")[0], entity_id=entity_id,
            image_entity=image_entity, incident_id=incident_id, critical=critical, announce=announce,
            respect_quiet_hours=respect_quiet_hours, ref=ref,
        )
        return incident_id

    def resolve(self, incident_id: int, note: str = "Afgesloten") -> None:
        self._db.add_incident_event(incident_id, note)
        self._db.update_incident(incident_id, status="resolved")

    def resolve_stale(self, hours: float = AUTO_RESOLVE_HOURS) -> int:
        limit = time.time() - hours * 3600
        stale = [i for i in self.active() if i["updated"] < limit]
        for incident in stale:
            self.resolve(incident["id"], "Automatisch afgesloten: geen nieuwe gebeurtenissen")
        return len(stale)
