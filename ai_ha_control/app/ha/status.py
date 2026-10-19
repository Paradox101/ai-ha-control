"""Expose the agent's state as Home Assistant entities, for dashboards and automations."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..autonomy import Autonomy
    from ..storage.db import Database
    from .client import HAClient
    from .index import EntityIndex

_LOGGER = logging.getLogger(__name__)

STATUS_ENTITY = "sensor.ai_huis_status"
MODE_ENTITY = "sensor.ai_huismodus"
CONCERN_ENTITY = "binary_sensor.ai_bezorgd"
LAST_INCIDENT_ENTITY = "sensor.ai_laatste_incident"
LEGACY_ENTITIES = (STATUS_ENTITY, MODE_ENTITY, CONCERN_ENTITY, LAST_INCIDENT_ENTITY)
# While the integration polls us it provides real entities; the state-only ones are then removed.
INTEGRATION_TIMEOUT = 300


async def ensure_kill_switch(ha: HAClient, index: EntityIndex, entity_id: str) -> None:
    """Create the kill switch input_boolean (switched on) when it does not exist yet."""
    if not entity_id.startswith("input_boolean.") or index.get(entity_id) is not None:
        return
    object_id = entity_id.split(".", 1)[1]
    try:
        await ha.create_input_boolean(object_id.replace("_", " ").replace("ai ", "AI ", 1))
        await ha.call_service("input_boolean", "turn_on", target={"entity_id": [entity_id]})
        _LOGGER.info("Created kill switch %s", entity_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not create kill switch %s: %s", entity_id, err)


class StatusPublisher:
    def __init__(self, ha: HAClient, db: Database, autonomy: Autonomy) -> None:
        self._ha = ha
        self._db = db
        self._autonomy = autonomy
        self._last: dict[str, Any] = {}
        self.integration_seen = 0.0  # monotonic time of the last poll by the integration
        self._legacy_removed = False

    def integration_polled(self) -> None:
        self.integration_seen = time.monotonic()

    @property
    def integration_active(self) -> bool:
        return bool(self.integration_seen) and time.monotonic() - self.integration_seen < INTEGRATION_TIMEOUT

    def state(self, mode: str, last_summary: str = "") -> dict[str, Any]:
        """Everything the dashboard entities show, for the integration and for publish()."""
        incidents = self._db.incidents(status="active")
        worst = max((i["severity"] for i in incidents), key=["info", "warning", "critical"].index, default=None)
        latest = self._db.incidents(limit=1)
        return {
            "status": {"critical": "alarm", "warning": "aandacht"}.get(worst or "", "rustig"),
            "mode": mode,
            "concern": worst in ("warning", "critical"),
            "open_incidents": len(incidents),
            "incidents": [i["title"] for i in incidents[:5]],
            "last_incident": latest[0]["title"][:250] if latest else None,
            "last_incident_severity": latest[0]["severity"] if latest else None,
            "last_incident_status": latest[0]["status"] if latest else None,
            "autonomous": self._autonomy.enabled,
            "last_check": last_summary[:250],
        }

    async def publish(self, mode: str, last_summary: str = "", health: dict[str, Any] | None = None) -> None:
        if self.integration_active:
            await self._remove_legacy()
            return
        self._legacy_removed = False
        st = self.state(mode, last_summary)
        status, incidents, worst = st["status"], st["incidents"], st["concern"]
        updates = {
            STATUS_ENTITY: (status, {
                "friendly_name": "AI huis status", "icon": "mdi:shield-home",
                "open_incidents": st["open_incidents"], "mode": mode,
                "autonomous": st["autonomous"], "last_check": st["last_check"], **(health or {}),
            }),
            MODE_ENTITY: (mode, {"friendly_name": "AI huismodus", "icon": "mdi:home-account"}),
            CONCERN_ENTITY: ("on" if worst else "off", {
                "friendly_name": "AI bezorgd", "device_class": "problem",
                "incidents": incidents,
            }),
            LAST_INCIDENT_ENTITY: (st["last_incident"] or "geen", {
                "friendly_name": "AI laatste incident", "icon": "mdi:alert-circle-outline",
                "severity": st["last_incident_severity"],
                "status": st["last_incident_status"],
            }),
        }
        for entity_id, (state, attributes) in updates.items():
            if self._last.get(entity_id) == (state, attributes):
                continue
            try:
                await self._ha.set_state(entity_id, state, attributes)
                self._last[entity_id] = (state, attributes)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not publish %s: %s", entity_id, err)

    async def _remove_legacy(self) -> None:
        if self._legacy_removed:
            return
        for entity_id in LEGACY_ENTITIES:
            try:
                await self._ha.delete_state(entity_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not remove %s: %s", entity_id, err)
        self._legacy_removed = True
        self._last.clear()

    def reset(self) -> None:
        """Force republishing (after a reconnect HA has forgotten these states)."""
        self._last.clear()
