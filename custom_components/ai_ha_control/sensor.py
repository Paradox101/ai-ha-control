"""Sensors: house status, house mode, last incident and AI response time."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AIHAConfigEntry
from .coordinator import AIHAEntity

STATUS_OPTIONS = ["rustig", "aandacht", "alarm"]


async def async_setup_entry(
    hass: HomeAssistant, entry: AIHAConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([
        StatusSensor(coordinator, "status"),
        ValueSensor(coordinator, "mode"),
        LastIncidentSensor(coordinator, "last_incident"),
        CountSensor(coordinator, "open_incidents"),
        CountSensor(coordinator, "pending_confirmations"),
        LLMSensor(coordinator, "llm_avg_seconds"),
    ])


class ValueSensor(AIHAEntity, SensorEntity):
    @property
    def native_value(self) -> Any:
        return self.coordinator.data.get(self._key)


class StatusSensor(ValueSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {"incidents": data.get("incidents"), "last_check": data.get("last_check")}


class LastIncidentSensor(ValueSensor):
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {"severity": data.get("last_incident_severity"), "status": data.get("last_incident_status")}


class CountSensor(ValueSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT


class LLMSensor(ValueSensor):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
