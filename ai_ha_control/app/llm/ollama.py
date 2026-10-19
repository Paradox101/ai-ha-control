"""Minimal Ollama client using the native /api/chat endpoint (tools + images)."""

from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

from ..config import Config

_LOGGER = logging.getLogger(__name__)


class LLMError(Exception):
    """Error talking to Ollama."""


class OllamaClient:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    @property
    def base_url(self) -> str:
        return self._config.ollama_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        timeout: float = 300,
    ) -> dict[str, Any]:
        """Run one non-streaming chat completion and return the assistant message."""
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

    async def describe_image(self, image: bytes, question: str) -> str:
        messages = [
            {
                "role": "user",
                "content": question,
                "images": [base64.b64encode(image).decode()],
            }
        ]
        message = await self.chat(messages, model=self._config.vision_model, timeout=180)
        return (message.get("content") or "").strip()

    async def status(self) -> dict[str, Any]:
        """Check connectivity and whether the configured models are installed."""
        try:
            async with self._session.get(
                f"{self.base_url}/api/tags", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
        except Exception as err:  # noqa: BLE001
            return {"reachable": False, "error": str(err)}
        installed = {m.get("name") for m in data.get("models", [])}

        def has(name: str) -> bool:
            return name in installed or f"{name}:latest" in installed

        return {
            "reachable": True,
            "model": self._config.model,
            "model_installed": has(self._config.model),
            "vision_model": self._config.vision_model,
            "vision_model_installed": has(self._config.vision_model),
        }
