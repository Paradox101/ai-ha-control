"""Minimal Ollama client using the native /api/chat endpoint (tools + images)."""

from __future__ import annotations

import asyncio
import base64
import heapq
import io
import itertools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from ..config import Config

_LOGGER = logging.getLogger(__name__)


class LLMError(Exception):
    """Error talking to Ollama."""


DeltaCallback = Callable[[str], Any]
# Content starting like this is probably a tool call written as text: never stream it.
NO_STREAM_PREFIXES = ("{", "`", "<")


# Camera snapshots are often 4K; vision models don't need that and the extra image tokens cost VRAM.
IMAGE_MAX_SIZE = 1280
IMAGE_RETRY_SIZE = 768
VISION_CONTEXT = 4096  # one picture + a short question; the chat context size is not needed here


def is_out_of_memory(err: Exception) -> bool:
    return "out of memory" in str(err).lower()


def is_too_large(err: Exception) -> bool:
    return "exceeds the available context size" in str(err).lower()


def shrink_image(image: bytes, max_size: int) -> bytes:
    """Downscale to max_size (longest side) as JPEG. Returns the original if it is small or unreadable."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as img:
            if max(img.size) <= max_size:
                return image
            img = img.convert("RGB")
            img.thumbnail((max_size, max_size))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=85)
            return out.getvalue()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not resize camera image: %s", err)
        return image


# Lower number = served first. One GPU, so requests are handled one at a time.
PRIORITY_SAFETY = 0
PRIORITY_CHAT = 1
PRIORITY_AGENT = 2
PRIORITY_PATROL = 3


class LLMStats:
    """Running totals per model: how long calls take, tokens, queueing and model swaps."""

    RECENT = 50
    SWAP_SECONDS = 1.0  # load_duration above this means the model had to be (re)loaded

    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {}
        self.recent: list[dict[str, Any]] = []
        self.waits: list[float] = []

    def record(self, model: str, seconds: float, data: dict[str, Any], error: bool = False) -> None:
        m = self.models.setdefault(model, {"calls": 0, "errors": 0, "seconds": 0.0, "prompt_tokens": 0,
                                           "output_tokens": 0, "loads": 0, "max_seconds": 0.0})
        m["calls"] += 1
        m["errors"] += int(error)
        m["seconds"] += seconds
        m["max_seconds"] = max(m["max_seconds"], seconds)
        m["prompt_tokens"] += int(data.get("prompt_eval_count") or 0)
        m["output_tokens"] += int(data.get("eval_count") or 0)
        loaded = (data.get("load_duration") or 0) / 1e9 > self.SWAP_SECONDS
        m["loads"] += int(loaded)
        self.recent.append({"ts": time.time(), "model": model, "seconds": round(seconds, 2), "error": error,
                            "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                            "loaded": loaded})
        del self.recent[:-self.RECENT]

    def record_wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        del self.waits[:-self.RECENT]

    def summary(self, waiting: int = 0) -> dict[str, Any]:
        models = {name: {**m, "avg_seconds": round(m["seconds"] / m["calls"], 2) if m["calls"] else 0,
                         "seconds": round(m["seconds"], 1), "max_seconds": round(m["max_seconds"], 1)}
                  for name, m in self.models.items()}
        durations = [r["seconds"] for r in self.recent if not r["error"]]
        return {
            "models": models,
            "recent": list(reversed(self.recent[-20:])),
            "queue": waiting,
            "avg_wait_seconds": round(sum(self.waits) / len(self.waits), 2) if self.waits else 0,
            "avg_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
        }


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
        self.stats = LLMStats()

    async def _acquire(self, priority: int) -> None:
        started = time.monotonic()
        await self._lock.acquire(priority)
        self.stats.record_wait(time.monotonic() - started)

    def performance(self) -> dict[str, Any]:
        return self.stats.summary(self._lock.waiting)

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
        on_delta: DeltaCallback | None = None,
    ) -> dict[str, Any]:
        """Run one chat completion and return the assistant message.

        With on_delta the answer is streamed: each piece of text is passed on as it arrives.
        """
        await self._acquire(priority)
        try:
            return await self._chat(messages, tools, model, timeout, on_delta=on_delta)
        finally:
            self._lock.release()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        timeout: float,
        context_size: int | None = None,
        on_delta: DeltaCallback | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": messages,
            "stream": on_delta is not None,
            "keep_alive": self._config.keep_alive,
            "think": self._config.think,
            "options": {"num_ctx": context_size or self._config.context_size, "temperature": 0.2},
        }
        if tools:
            payload["tools"] = tools
        if schema:
            payload["format"] = schema
        started = time.monotonic()
        try:
            async with self._session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    raise LLMError(f"Ollama returned {resp.status}: {await resp.text()}")
                if on_delta is None:
                    data = await resp.json()
                    message = data.get("message") or {}
                else:
                    data, message = await self._read_stream(resp, on_delta)
        except aiohttp.ClientError as err:
            self.stats.record(payload["model"], time.monotonic() - started, {}, error=True)
            raise LLMError(f"Cannot reach Ollama at {self.base_url}: {err}") from err
        except LLMError:
            self.stats.record(payload["model"], time.monotonic() - started, {}, error=True)
            raise
        self.stats.record(payload["model"], time.monotonic() - started, data)
        _LOGGER.debug(
            "LLM %s: eval_count=%s prompt_eval_count=%s",
            payload["model"],
            data.get("eval_count"),
            data.get("prompt_eval_count"),
        )
        return message

    @staticmethod
    async def _read_stream(resp: aiohttp.ClientResponse, on_delta: DeltaCallback) -> tuple[dict, dict]:
        """Read Ollama's NDJSON stream; returns (final chunk, combined message)."""
        content, tool_calls, final = "", [], {}
        streaming: bool | None = None  # decided on the first visible character
        async for raw in resp.content:
            line = raw.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise LLMError(f"Ollama error: {chunk['error']}")
            message = chunk.get("message") or {}
            piece = message.get("content") or ""
            tool_calls.extend(message.get("tool_calls") or [])
            if piece:
                content += piece
                if streaming is None and content.strip():
                    streaming = not content.lstrip().startswith(NO_STREAM_PREFIXES)
                    piece = content  # include the leading whitespace-only pieces held back so far
                if streaming:
                    result = on_delta(piece)
                    if asyncio.iscoroutine(result):
                        await result
            if chunk.get("done"):
                final = chunk
                break
        message = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return final, message

    async def describe_image(self, image: bytes, question: str, priority: int = PRIORITY_CHAT,
                             schema: dict[str, Any] | None = None) -> str:
        """Ask the vision model about an image. With a JSON schema the reply is JSON in that shape."""
        model = self._config.vision_model
        context = min(self._config.context_size, VISION_CONTEXT)

        def request(max_size: int) -> list[dict[str, Any]]:
            data = base64.b64encode(shrink_image(image, max_size)).decode()
            return [{"role": "user", "content": question, "images": [data]}]

        await self._acquire(priority)
        try:
            try:
                message = await self._chat(request(IMAGE_MAX_SIZE), None, model, 180, context, schema=schema)
            except LLMError as err:
                if is_out_of_memory(err):
                    # Usually the chat model still occupies the GPU: free it and try once more, smaller.
                    _LOGGER.warning("Vision model ran out of GPU memory, unloading other models and retrying")
                    await self.unload_others(model)
                elif is_too_large(err):
                    _LOGGER.warning("Camera image too large for the vision context, retrying smaller")
                else:
                    raise
                message = await self._chat(request(IMAGE_RETRY_SIZE), None, model, 180, context, schema=schema)
        finally:
            self._lock.release()
        return (message.get("content") or "").strip()

    async def unload_others(self, keep: str) -> list[str]:
        """Ask Ollama to unload every running model except `keep` (frees VRAM)."""
        try:
            async with self._session.get(f"{self.base_url}/api/ps", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                running = [m.get("name") or m.get("model") for m in (await resp.json()).get("models", [])]
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Cannot list running models: %s", err)
            return []
        unloaded = []
        for name in running:
            if not name or name in (keep, f"{keep}:latest"):
                continue
            try:
                async with self._session.post(f"{self.base_url}/api/generate", json={"model": name, "keep_alive": 0},
                                              timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    await resp.read()
                unloaded.append(name)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Cannot unload %s: %s", name, err)
        return unloaded

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
