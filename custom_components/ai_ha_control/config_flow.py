"""Config flow: automatic via Supervisor discovery, or manual URL + API key."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AIHAAuthError, AIHAClient, AIHAError
from .const import CONF_API_KEY, DEFAULT_PORT, DOMAIN

try:
    from homeassistant.helpers.service_info.hassio import HassioServiceInfo
except ImportError:  # Home Assistant < 2025.1
    from homeassistant.components.hassio import HassioServiceInfo

TITLE = "AI HA Control"


class AIHAConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}

    async def _validate(self, url: str, api_key: str) -> str | None:
        client = AIHAClient(async_get_clientsession(self.hass), url, api_key)
        try:
            await client.status()
        except AIHAAuthError:
            return "invalid_auth"
        except AIHAError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            error = await self._validate(url, user_input[CONF_API_KEY])
            if error is None:
                return self.async_create_entry(
                    title=TITLE, data={CONF_URL: url, CONF_API_KEY: user_input[CONF_API_KEY]}
                )
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=f"http://local-ai-ha-control:{DEFAULT_PORT}"): str,
                vol.Required(CONF_API_KEY): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        config = discovery_info.config
        self._discovered = {
            CONF_URL: f"http://{config['host']}:{config.get('port', DEFAULT_PORT)}",
            CONF_API_KEY: config["api_key"],
        }
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates=self._discovered)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._validate(self._discovered[CONF_URL], self._discovered[CONF_API_KEY])
            if error is None:
                return self.async_create_entry(title=TITLE, data=self._discovered)
            errors["base"] = error
        return self.async_show_form(step_id="hassio_confirm", errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            error = await self._validate(entry.data[CONF_URL], user_input[CONF_API_KEY])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )
