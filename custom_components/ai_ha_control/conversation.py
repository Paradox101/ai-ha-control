"""Conversation agent that forwards Assist requests to the AI HA Control add-on."""

from __future__ import annotations

from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from . import AIHAConfigEntry
from .api import AIHAError
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: AIHAConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AIHAConversationEntity(entry)])


class AIHAConversationEntity(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: AIHAConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AI HA Control",
            manufacturer="AI HA Control",
            model="Lokale AI (Ollama)",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> str:
        return MATCH_ALL

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        conversation_id = user_input.conversation_id or ulid.ulid_now()
        try:
            result = await self._entry.runtime_data.converse(user_input.text, conversation_id, user_input.language)
            speech = result.get("speech") or "…"
        except AIHAError as err:
            speech = f"Sorry, de AI is niet bereikbaar ({err})."
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        return ConversationResult(response=response, conversation_id=conversation_id)
