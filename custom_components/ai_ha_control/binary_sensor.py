"""Binary sensors: the AI is concerned, and the add-on has a problem."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AIHAConfigEntry
from .coordinator import AIHAEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: AIHAConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([ConcernSensor(coordinator, "concern"), ProblemSensor(coordinator, "addon_problem")])


class ConcernSensor(AIHAEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("concern"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"incidents": self.coordinator.data.get("incidents")}


class ProblemSensor(AIHAEntity, BinarySensorEntity):
    """On when the add-on cannot be reached or reports a health problem."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True  # this sensor reports exactly the case where the add-on is unavailable

    @property
    def is_on(self) -> bool:
        if not self.coordinator.last_update_success or not self.coordinator.data:
            return True
        return bool(self.coordinator.data.get("health_problems"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {"problems": data.get("health_problems") or ([] if self.coordinator.last_update_success
                                                            else ["addon_unreachable"])}
