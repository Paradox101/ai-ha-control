"""AI HA Control: use the local AI add-on as an Assist conversation agent."""

from __future__ import annotations

from dataclasses import dataclass

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AIHAAuthError, AIHAClient, AIHAError
from .const import CONF_API_KEY, DOMAIN
from .coordinator import AIHACoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CONVERSATION, Platform.SENSOR, Platform.SWITCH]
SERVICE_ASK = "ask"
ASK_SCHEMA = vol.Schema(
    {vol.Required("text"): cv.string, vol.Optional("conversation_id"): cv.string}
)



@dataclass
class AIHAData:
    client: AIHAClient
    coordinator: AIHACoordinator


type AIHAConfigEntry = ConfigEntry[AIHAData]


async def async_setup_entry(hass: HomeAssistant, entry: AIHAConfigEntry) -> bool:
    client = AIHAClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_API_KEY])
    try:
        await client.status()
    except AIHAAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except AIHAError as err:
        raise ConfigEntryNotReady(f"AI HA Control add-on not reachable: {err}") from err
    coordinator = AIHACoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = AIHAData(client, coordinator)

    async def handle_ask(call: ServiceCall) -> ServiceResponse:
        try:
            result = await client.converse(call.data["text"], call.data.get("conversation_id"))
        except AIHAError as err:
            raise HomeAssistantError(str(err)) from err
        return {"answer": result.get("speech", ""), "conversation_id": result.get("conversation_id")}

    if not hass.services.has_service(DOMAIN, SERVICE_ASK):
        hass.services.async_register(
            DOMAIN, SERVICE_ASK, handle_ask, schema=ASK_SCHEMA, supports_response=SupportsResponse.ONLY
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AIHAConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.services.async_remove(DOMAIN, SERVICE_ASK)
    return unloaded
