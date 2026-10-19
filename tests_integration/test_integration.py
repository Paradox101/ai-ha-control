from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.components import conversation
from homeassistant.const import CONF_URL
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.ai_ha_control.api import AIHAAuthError, AIHAError
from custom_components.ai_ha_control.config_flow import HassioServiceInfo
from custom_components.ai_ha_control.const import CONF_API_KEY, DOMAIN

CLIENT = "custom_components.ai_ha_control.api.AIHAClient"
STATE = {
    "status": "aandacht", "mode": "weg", "concern": True, "open_incidents": 1, "incidents": ["Raam open"],
    "last_incident": "Raam open", "last_incident_severity": "warning", "last_incident_status": "open",
    "autonomous": True, "last_check": "Alles rustig", "kill_switch": True,
    "kill_switch_entity": "input_boolean.ai_autonoom", "llm_avg_seconds": 2.5,
    "llm_queue": 0, "pending_confirmations": 0, "health_problems": [],
}


def stream_of(*items):
    calls = []

    async def converse_stream(*args):
        calls.append(args)
        for item in items:
            yield item

    converse_stream.calls = calls
    return converse_stream


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
    stream = stream_of({"delta": "De keukenlamp "}, {"delta": "is aan."},
                       {"done": True, "speech": "De keukenlamp is aan.", "streamed": True})
    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), patch(f"{CLIENT}.converse", converse), \
            patch(f"{CLIENT}.state", AsyncMock(return_value=dict(STATE))), patch(f"{CLIENT}.converse_stream", stream):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
        assert entity_id
        result = await conversation.async_converse(
            hass, "Doe de keukenlamp aan", None, Context(), language="nl", agent_id=entity_id
        )
        assert result.response.speech["plain"]["speech"] == "De keukenlamp is aan."
        assert stream.calls[0][1] == "Doe de keukenlamp aan"
        assert stream.calls[0][5] is None  # no device: no area

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


async def test_satellite_area_and_unstreamed_answer(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://addon:8099", CONF_API_KEY: "k"})
    entry.add_to_hass(hass)
    kitchen = ar.async_get(hass).async_create("Keuken")
    satellite = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "satelliet")}, name="Satelliet")
    dr.async_get(hass).async_update_device(satellite.id, area_id=kitchen.id)
    stream = stream_of({"done": True, "speech": "Gedaan: keukenlamp aan", "streamed": False})
    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), \
            patch(f"{CLIENT}.state", AsyncMock(return_value=dict(STATE))), patch(f"{CLIENT}.converse_stream", stream):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
        result = await conversation.async_converse(
            hass, "ja", None, Context(), language="nl", agent_id=entity_id, device_id=satellite.id
        )
    assert result.response.speech["plain"]["speech"] == "Gedaan: keukenlamp aan"
    assert stream.calls[0][5] == "Keuken"


async def test_entities_from_addon_state(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://addon:8099", CONF_API_KEY: "k"})
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "input_boolean", "turn_off")
    with patch(f"{CLIENT}.status", AsyncMock(return_value={})), \
            patch(f"{CLIENT}.state", AsyncMock(return_value=dict(STATE))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)

        def state_of(domain: str, key: str):
            entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{key}")
            assert entity_id, key
            return hass.states.get(entity_id)

        assert state_of("sensor", "status").state == "aandacht"
        assert state_of("sensor", "mode").state == "weg"
        assert state_of("sensor", "last_incident").attributes["severity"] == "warning"
        assert state_of("sensor", "llm_avg_seconds").state == "2.5"
        assert state_of("binary_sensor", "concern").state == "on"
        assert state_of("binary_sensor", "addon_problem").state == "off"
        switch = state_of("switch", "autonomous")
        assert switch.state == "on"
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch.entity_id}, blocking=True)
        assert calls[0].data["entity_id"] == "input_boolean.ai_autonoom"
    assert await hass.config_entries.async_unload(entry.entry_id)
