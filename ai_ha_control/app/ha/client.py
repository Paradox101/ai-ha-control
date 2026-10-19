"""Home Assistant WebSocket + REST client."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..config import Config

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class HAError(Exception):
    """Error returned by Home Assistant."""


class HAClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._event_callbacks: dict[str, list[EventCallback]] = defaultdict(list)
        self._connect_callbacks: list[Callable[[], Awaitable[None]]] = []
        self._connected = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._callback_tasks: set[asyncio.Task] = set()
        self.ha_version: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_event(self, event_type: str, callback: EventCallback) -> None:
        """Register a callback receiving the event data; register before start()."""
        self._event_callbacks[event_type].append(callback)

    def on_state_changed(self, callback: EventCallback) -> None:
        self.on_event("state_changed", callback)

    def on_connect(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._connect_callbacks.append(callback)

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def _run_forever(self) -> None:
        delay = 1
        while True:
            try:
                await self._connect_and_listen()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Home Assistant connection lost: %s", err)
            self._connected.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("connection lost"))
            self._pending.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _connect_and_listen(self) -> None:
        url = self._config.ha_ws_url
        _LOGGER.info("Connecting to Home Assistant at %s", url)
        async with self._session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
            self._ws = ws
            msg = await ws.receive_json()
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected handshake: {msg}")
            await ws.send_json({"type": "auth", "access_token": self._config.ha_token})
            msg = await ws.receive_json()
            if msg.get("type") != "auth_ok":
                hint = "" if self._config.ha_token else " (no token: SUPERVISOR_TOKEN missing)"
                raise HAError(f"authentication failed: {msg.get('message', msg)}{hint}")
            self.ha_version = msg.get("ha_version")
            _LOGGER.info("Connected to Home Assistant %s", self.ha_version)

            reader = asyncio.create_task(self._reader(ws))
            try:
                for event_type in {"state_changed", *self._event_callbacks}:
                    await self._send_command({"type": "subscribe_events", "event_type": event_type})
                self._connected.set()
                for callback in self._connect_callbacks:
                    await callback()
                await reader
            finally:
                reader.cancel()

    async def _reader(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = raw.json()
            for msg in data if isinstance(data, list) else [data]:
                await self._dispatch(msg)
        raise HAError("websocket closed")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "event":
            event = msg.get("event", {})
            event_type = event.get("event_type")
            data = event.get("data", {})
            if event.get("context") and isinstance(data, dict) and "context" not in data:
                data = {**data, "context": event["context"]}  # who caused it (see ha/context.py)
            for callback in self._event_callbacks.get(event_type, []):
                try:
                    result = callback(data)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("%s callback failed", event_type)
                    continue
                if asyncio.iscoroutine(result):
                    # Never await here: the reader must keep reading, or commands sent
                    # from the callback would wait for their reply forever.
                    task = asyncio.create_task(self._run_callback(result, event_type))
                    self._callback_tasks.add(task)
                    task.add_done_callback(self._callback_tasks.discard)
            return
        fut = self._pending.pop(msg.get("id"), None)
        if fut is None or fut.done():
            return
        if msg.get("success", True):
            fut.set_result(msg.get("result"))
        else:
            error = msg.get("error") or {}
            fut.set_exception(HAError(f"{error.get('code')}: {error.get('message')}"))

    @staticmethod
    async def _run_callback(coro: Awaitable[None], event_type: str) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s callback failed", event_type)

    async def _send_command(self, payload: dict[str, Any], timeout: float = 30) -> Any:
        if self._ws is None or self._ws.closed:
            raise HAError("not connected to Home Assistant")
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({**payload, "id": msg_id})
        return await asyncio.wait_for(fut, timeout)

    async def command(self, payload: dict[str, Any], timeout: float = 30) -> Any:
        """Send a WebSocket command once connected and return its result."""
        await self.wait_connected(timeout)
        return await self._send_command(payload, timeout)

    # --- Convenience wrappers -------------------------------------------------

    async def get_states(self) -> list[dict[str, Any]]:
        return await self.command({"type": "get_states"})

    async def get_services(self) -> dict[str, dict[str, Any]]:
        return await self.command({"type": "get_services"})

    async def get_config(self) -> dict[str, Any]:
        return await self.command({"type": "get_config"})

    async def area_registry(self) -> list[dict[str, Any]]:
        return await self.command({"type": "config/area_registry/list"})

    async def device_registry(self) -> list[dict[str, Any]]:
        return await self.command({"type": "config/device_registry/list"})

    async def entity_registry(self) -> list[dict[str, Any]]:
        entries = await self.command({"type": "config/entity_registry/list"})
        # The list only has the short form; aliases are in the extended entries.
        try:
            extended = await self.command(
                {"type": "config/entity_registry/get_entries", "entity_ids": [e["entity_id"] for e in entries]},
                timeout=60,
            )
        except (HAError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Could not read entity aliases: %s", err)
            return entries
        for entry in entries:
            details = (extended or {}).get(entry["entity_id"]) or {}
            entry["aliases"] = details.get("aliases") or []
        return entries

    async def exposed_entities(self) -> set[str]:
        """Entities exposed to Assist; empty (nothing allowed) when the list cannot be read."""
        try:
            result = await self.command({"type": "homeassistant/expose_entity/list"})
        except (HAError, asyncio.TimeoutError) as err:
            _LOGGER.error("Could not read exposed entities, exposing nothing: %s", err)
            return set()
        exposed = result.get("exposed_entities", {})
        return {eid for eid, assistants in exposed.items() if assistants.get("conversation")}

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> Any:
        payload: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if data:
            payload["service_data"] = data
        if target:
            payload["target"] = target
        if return_response:
            payload["return_response"] = True
            result = await self.command(payload)
            return (result or {}).get("response")
        return await self.command(payload)

    async def history(
        self, entity_ids: list[str], start_time: str, end_time: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "type": "history/history_during_period",
            "start_time": start_time,
            "entity_ids": entity_ids,
            "minimal_response": True,
            "no_attributes": True,
            "significant_changes_only": False,
        }
        if end_time:
            payload["end_time"] = end_time
        return await self.command(payload, timeout=60)

    async def logbook(self, entity_ids: list[str], start_time: str, end_time: str) -> list[dict[str, Any]]:
        return await self.command(
            {"type": "logbook/get_events", "start_time": start_time, "end_time": end_time, "entity_ids": entity_ids},
            timeout=120,
        )

    async def related(self, item_type: str, item_id: str) -> dict[str, list[str]]:
        """Things that reference an item (HA search), e.g. automations that use an entity."""
        return await self.command({"type": "search/related", "item_type": item_type, "item_id": item_id})

    async def validate_config(self, triggers: Any, conditions: Any, actions: Any) -> dict[str, Any]:
        """Validate automation parts; returns {section: {valid, error}}."""
        payload: dict[str, Any] = {"type": "validate_config", "triggers": triggers, "actions": actions}
        if conditions:
            payload["conditions"] = conditions
        return await self.command(payload)

    # --- REST -------------------------------------------------------------------

    async def _rest(self, method: str, path: str, json: Any = None, raw: bool = False) -> Any:
        url = f"{self._config.ha_url.rstrip('/')}/api/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._config.ha_token}"}
        async with self._session.request(
            method, url, json=json, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status >= 400:
                raise HAError(f"{method} {path} failed ({resp.status}): {(await resp.text())[:300]}")
            if raw:
                return await resp.read()
            return await resp.json(content_type=None)

    async def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> Any:
        """Create/update a state-only entity (not stored by HA across restarts; republished on connect)."""
        return await self._rest("POST", f"states/{entity_id}", json={"state": state, "attributes": attributes})

    async def create_input_boolean(self, name: str, icon: str = "mdi:robot") -> Any:
        return await self.command({"type": "input_boolean/create", "name": name, "icon": icon})

    async def camera_snapshot(self, entity_id: str) -> bytes:
        return await self._rest("GET", f"camera_proxy/{entity_id}", raw=True)

    async def get_automation_config(self, automation_id: str) -> dict[str, Any]:
        return await self._rest("GET", f"config/automation/config/{automation_id}")

    async def save_automation_config(self, automation_id: str, config: dict[str, Any]) -> Any:
        """Create or update an automation in automations.yaml (HA reloads automatically)."""
        return await self._rest("POST", f"config/automation/config/{automation_id}", json=config)
