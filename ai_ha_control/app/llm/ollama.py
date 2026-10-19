"""Minimal Ollama client using the native /api/chat endpoint (tools + images)."""

from __future__ import annotations

import asyncio
import base64
import heapq
import itertools
import logging
from typing import Any

import aiohttp

from ..config import Config

_LOGGER = logging.getLogger(__name__)


class LLMError(Exception):
    """Error talking to Ollama."""


# Lower number = served first. One GPU, so requests are handled one at a time.
PRIORITY_SAFETY = 0
PRIORITY_CHAT = 1
PRIORITY_AGENT = 2
PRIORITY_PATROL = 3


class PriorityLock:
    """A lock that hands over to the waiting request with the best (lowest) priority."""

    def __init__(self) -> None:
        self._locked = False
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._order = itertools.count()

    async def acquire(self, priority: int) -> None:
        if not self._locked and not self._waiters:
            self._locked = True
            return
        fut = asyncio.get_running_loop().create_future()
        entry = (priority, next(self._order), fut)
        heapq.heappush(self._waiters, entry)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()  # we were handed the lock but are cancelled: pass it on
            else:
                self._waiters.remove(entry)
                heapq.heapify(self._waiters)
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)  # lock stays taken, ownership moves to the waiter
                return
        self._locked = False

    @property
    def waiting(self) -> int:
        return len(self._waiters)


class OllamaClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session
        self._lock = PriorityLock()
        self._capabilities: dict[str, list[str] | None] = {}

    @property
    def base_url(self) -> str:
        return self._config.ollama_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        timeout: float = 300,
        priority: int = PRIORITY_CHAT,
    ) -> dict[str, Any]:
        """Run one non-streaming chat completion and return the assistant message."""
        await self._lock.acquire(priority)
        try:
            return await self._chat(messages, tools, model, timeout)
        finally:
            self._lock.release()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self._config.keep_alive,
            "think": self._config.think,
            "options": {"num_ctx": self._config.context_size, "temperature": 0.2},
        }
        if tools:
            payload["tools"] = tools
        try:
            async with self._session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    raise LLMError(f"Ollama returned {resp.status}: {await resp.text()}")
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise LLMError(f"Cannot reach Ollama at {self.base_url}: {err}") from err
        message = data.get("message") or {}
        _LOGGER.debug(
            "LLM %s: eval_count=%s prompt_eval_count=%s",
            payload["model"],
            data.get("eval_count"),
            data.get("prompt_eval_count"),
        )
        return message

    async def describe_image(self, image: bytes, question: str, priority: int = PRIORITY_CHAT) -> str:
        messages = [
            {
                "role": "user",
                "content": question,
                "images": [base64.b64encode(image).decode()],
            }
        ]
        message = await self.chat(messages, model=self._config.vision_model, timeout=180, priority=priority)
        return (message.get("content") or "").strip()

    async def status(self, url: str | None = None) -> dict[str, Any]:
        """Check connectivity and whether the configured models are installed."""
        base = (url or self.base_url).rstrip("/")
        try:
            async with self._session.get(f"{base}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            return {"reachable": False, "error": str(err) or type(err).__name__}
        installed = {m.get("name") for m in data.get("models", [])}

        def has(name: str) -> bool:
            return name in installed or f"{name}:latest" in installed

        return {
            "reachable": True,
            "model_count": len(installed),
            "model": self._config.model,
            "model_installed": has(self._config.model),
            "vision_model": self._config.vision_model,
            "vision_model_installed": has(self._config.vision_model),
        }

    async def models(self) -> list[dict[str, Any]]:
        """Installed models with size and capabilities (tools/vision) where Ollama reports them."""
        try:
            async with self._session.get(f"{self.base_url}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                tags = (await resp.json()).get("models", [])
        except Exception:  # noqa: BLE001
            return []
        result = []
        for tag in sorted(tags, key=lambda t: t.get("name", "")):
            name = tag.get("name", "")
            if name not in self._capabilities:
                try:
                    async with self._session.post(f"{self.base_url}/api/show", json={"model": name},
                                                  timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        self._capabilities[name] = (await resp.json()).get("capabilities")
                except Exception:  # noqa: BLE001
                    self._capabilities[name] = None
            caps = self._capabilities[name]
            result.append({
                "name": name,
                "size_gb": round(tag.get("size", 0) / 1e9, 1),
                "tools": None if caps is None else "tools" in caps,
                "vision": None if caps is None else "vision" in caps,
            })
        return result
