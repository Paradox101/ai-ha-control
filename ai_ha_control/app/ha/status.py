"""Expose the agent's state as Home Assistant entities, for dashboards and automations."""

from __future__ import annotations

import logging
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

    async def publish(self, mode: str, last_summary: str = "") -> None:
        incidents = self._db.incidents(status="active")
        worst = max((i["severity"] for i in incidents), key=["info", "warning", "critical"].index, default=None)
        status = {"critical": "alarm", "warning": "aandacht"}.get(worst or "", "rustig")
        latest = self._db.incidents(limit=1)
        updates = {
            STATUS_ENTITY: (status, {
                "friendly_name": "AI huis status", "icon": "mdi:shield-home",
                "open_incidents": len(incidents), "mode": mode,
                "autonomous": self._autonomy.enabled, "last_check": last_summary[:250],
            }),
            MODE_ENTITY: (mode, {"friendly_name": "AI huismodus", "icon": "mdi:home-account"}),
            CONCERN_ENTITY: ("on" if worst in ("warning", "critical") else "off", {
                "friendly_name": "AI bezorgd", "device_class": "problem",
                "incidents": [i["title"] for i in incidents[:5]],
            }),
            LAST_INCIDENT_ENTITY: (latest[0]["title"][:250] if latest else "geen", {
                "friendly_name": "AI laatste incident", "icon": "mdi:alert-circle-outline",
                "severity": latest[0]["severity"] if latest else None,
                "status": latest[0]["status"] if latest else None,
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

    def reset(self) -> None:
        """Force republishing (after a reconnect HA has forgotten these states)."""
        self._last.clear()
