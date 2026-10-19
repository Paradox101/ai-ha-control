"""Conversation agent that forwards Assist requests to the AI HA Control add-on."""

from __future__ import annotations

from collections.abc import AsyncIterator

from homeassistant.components import conversation
from homeassistant.components.conversation import ChatLog, ConversationEntity, ConversationInput, ConversationResult
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AIHAConfigEntry
from .api import AIHAError
from .coordinator import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: AIHAConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AIHAConversationEntity(entry)])


def device_area(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Name of the area of the device (voice satellite) the request came from."""
    if not device_id or (device := dr.async_get(hass).async_get(device_id)) is None or not device.area_id:
        return None
    area = ar.async_get(hass).async_get_area(device.area_id)
    return area.name if area else None


class AIHAConversationEntity(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: AIHAConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = device_info(entry)

    @property
    def supported_languages(self) -> str:
        return MATCH_ALL

    async def _async_handle_message(self, user_input: ConversationInput, chat_log: ChatLog) -> ConversationResult:
        area = device_area(self.hass, user_input.device_id)
        user_id = user_input.context.user_id if user_input.context else None
        stream = self._entry.runtime_data.client.converse_stream(
            user_input.text, chat_log.conversation_id, user_input.language, user_id, area
        )

        async def deltas() -> AsyncIterator[dict]:
            yield {"role": "assistant"}
            streamed = False
            try:
                async for item in stream:
                    if piece := item.get("delta"):
                        streamed = True
                        yield {"content": piece}
                    elif item.get("done") and not streamed:
                        # Nothing was streamed (e.g. a yes/no confirmation): send the whole answer.
                        yield {"content": item.get("speech") or "…"}
            except AIHAError as err:
                yield {"content": ("" if not streamed else " ") + f"Sorry, de AI is niet bereikbaar ({err})."}

        async for _content in chat_log.async_add_delta_content_stream(self.entity_id, deltas()):
            pass
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
