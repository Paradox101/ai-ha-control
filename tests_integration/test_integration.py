from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.components import conversation
from homeassistant.const import CONF_URL
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ai_ha_control.api import AIHAAuthError, AIHAError
from custom_components.ai_ha_control.config_flow import HassioServiceInfo
from custom_components.ai_ha_control.const import CONF_API_KEY, DOMAIN

CLIENT = "custom_components.ai_ha_control.api.AIHAClient"


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    with patch(f"{CLIENT}.status", AsyncMock(side_effect=AIHAAuthError("bad"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://addon:8099/", CONF_API_KEY: "fout"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(f"{CLIENT}.status", AsyncMock(side_effect=AIHAError("down"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://addon:8099/", CONF_API_KEY: "k"}
        )
    assert result["errors"] == {"base": "cannot_connect"}

    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), \
            patch("custom_components.ai_ha_control.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://addon:8099/", CONF_API_KEY: "k"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: "http://addon:8099", CONF_API_KEY: "k"}


async def test_hassio_discovery(hass: HomeAssistant) -> None:
    info = HassioServiceInfo(
        config={"host": "abc123-ai-ha-control", "port": 8099, "api_key": "geheim"},
        name="AI HA Control",
        slug="abc123_ai_ha_control",
        uuid="1234",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), \
            patch("custom_components.ai_ha_control.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: "http://abc123-ai-ha-control:8099", CONF_API_KEY: "geheim"}


async def test_conversation_agent_and_ask_service(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://addon:8099", CONF_API_KEY: "k"})
    entry.add_to_hass(hass)
    converse = AsyncMock(return_value={"speech": "De keukenlamp is aan.", "conversation_id": "c1"})
    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), patch(f"{CLIENT}.converse", converse):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
        assert entity_id
        result = await conversation.async_converse(
            hass, "Doe de keukenlamp aan", None, Context(), language="nl", agent_id=entity_id
        )
        assert result.response.speech["plain"]["speech"] == "De keukenlamp is aan."
        assert converse.call_args.args[0] == "Doe de keukenlamp aan"

        response = await hass.services.async_call(
            DOMAIN, "ask", {"text": "Hoe warm is het?"}, blocking=True, return_response=True
        )
        assert response == {"answer": "De keukenlamp is aan.", "conversation_id": "c1"}

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_retries_when_addon_down(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://addon:8099", CONF_API_KEY: "k"})
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.status", AsyncMock(side_effect=AIHAError("down"))):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY
