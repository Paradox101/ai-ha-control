"""Client for the AI HA Control add-on API."""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT


class AIHAError(Exception):
    """Cannot talk to the add-on."""


class AIHAAuthError(AIHAError):
    """The API key was rejected."""


class AIHAClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _request(self, method: str, path: str, json: Any = None, timeout: float = 15) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._url}{path}",
                json=json,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (401, 403):
                    raise AIHAAuthError("invalid API key")
                if resp.status >= 400:
                    raise AIHAError(f"add-on returned {resp.status}")
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AIHAError(str(err) or type(err).__name__) from err

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def converse(self, text: str, conversation_id: str | None, language: str | None = None) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/conversation",
            {"text": text, "conversation_id": conversation_id, "language": language},
            timeout=REQUEST_TIMEOUT,
        )
