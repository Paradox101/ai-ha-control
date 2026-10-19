"""Tool framework: definitions the LLM sees and the context they run in."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Config
    from ..ha.client import HAClient
    from ..ha.index import EntityIndex
    from ..llm.ollama import OllamaClient
    from ..notify import Notifier
    from ..policy import Policy
    from ..storage.db import Database
    from ..autonomy import Autonomy
    from ..ha.context import ContextTracker
    from ..watcher.incidents import IncidentManager
    from .actions import ConfirmationStore

_LOGGER = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000


@dataclass
class ToolContext:
    config: Config
    ha: HAClient
    index: EntityIndex
    llm: OllamaClient
    policy: Policy
    db: Database
    confirmations: ConfirmationStore
    notifier: Notifier | None = None
    conversation_id: str | None = None
    timezone: str | None = None
    source: str = "web"  # web | assist | watcher | autonomous
    autonomy: Autonomy | None = None
    incidents: IncidentManager | None = None
    reason: str = ""  # why the autonomous agent is acting (shown with its actions)
    contexts: ContextTracker | None = None  # who caused a state change


Handler = Callable[..., Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        """All schemas, or only those in names (run() still accepts every tool)."""
        return [tool.schema() for tool in self._tools.values() if names is None or tool.name in names]

    async def run(self, ctx: ToolContext, name: str, arguments: Any) -> str:
        """Run a tool and return its result as a (truncated) JSON string."""
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"onbekende tool '{name}'", "available": self.names})
        for _ in range(2):  # small models sometimes encode the arguments twice
            if not isinstance(arguments, str):
                break
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return json.dumps({"error": "argumenten zijn geen geldige JSON"})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return json.dumps({"error": "argumenten moeten een JSON-object zijn"})
        allowed = set(tool.parameters.get("properties", {}))
        arguments = {k: v for k, v in arguments.items() if k in allowed}
        try:
            result = await tool.handler(ctx, **arguments)
        except TypeError as err:
            return json.dumps({"error": f"ongeldige argumenten: {err}"})
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Tool %s failed", name)
            return json.dumps({"error": str(err)}, ensure_ascii=False)
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "… (ingekort)"
        return text


def as_list(value: Any) -> list[str]:
    """Small models sometimes pass a string where a list is expected."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]
