"""Switch: let the AI act on its own.

Mirrors the add-on's kill switch (an input_boolean in Home Assistant itself), so it works without
giving the integration's API key any control over the add-on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AIHAConfigEntry
from .coordinator import AIHAEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AIHAConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AutonomySwitch(entry.runtime_data.coordinator, "autonomous")])


class AutonomySwitch(AIHAEntity, SwitchEntity):
    _attr_icon = "mdi:robot"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.get("kill_switch") is not None

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("kill_switch"))

    async def _set(self, on: bool) -> None:
        entity_id = self.coordinator.data.get("kill_switch_entity")
        if not entity_id:
            raise HomeAssistantError("geen noodknop ingesteld in de add-on")
        domain = entity_id.split(".", 1)[0]
        await self.hass.services.async_call(domain, "turn_on" if on else "turn_off", {"entity_id": entity_id},
                                            blocking=True)
        self.coordinator.data["kill_switch"] = on
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
