"""Polls the add-on for the state behind the sensors and the autonomy switch."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from .api import AIHAAuthError, AIHAClient, AIHAError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


class AIHACoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: AIHAClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.state()
        except AIHAAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err  # starts the reauth flow
        except AIHAError as err:
            raise UpdateFailed(str(err)) from err


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="AI HA Control",
        manufacturer="AI HA Control",
        model="Lokale AI (Ollama)",
        entry_type=DeviceEntryType.SERVICE,
    )


class AIHAEntity(CoordinatorEntity[AIHACoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AIHACoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)
