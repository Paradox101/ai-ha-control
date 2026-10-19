"""Who changed something: the agent, the AI on request, an automation, a resident or the device itself.

Home Assistant attaches a context to every state change. Service calls we make ourselves return their
context id; automations announce theirs in the automation_triggered event; a context with a user id
and no parent comes from a person (dashboard, app, voice).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

AGENT, AI, AUTOMATION, RESIDENT, DEVICE = "agent", "ai", "automatisering", "bewoner", "apparaat"
MAX_CONTEXTS = 2000


class ContextTracker:
    def __init__(self) -> None:
        self._known: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.own_user_id: str | None = None

    def remember(self, context_id: str | None, origin: str, label: str = "") -> None:
        if not context_id:
            return
        self._known[context_id] = (origin, label)
        self._known.move_to_end(context_id)
        while len(self._known) > MAX_CONTEXTS:
            self._known.popitem(last=False)

    def note_automation(self, data: dict[str, Any]) -> None:
        context = data.get("context") or {}
        self.remember(context.get("id"), AUTOMATION, data.get("name") or data.get("entity_id") or "")

    def classify(self, context: dict[str, Any] | None) -> tuple[str, str]:
        """(origin, label) of a state change."""
        context = context or {}
        for key in ("id", "parent_id"):
            known = self._known.get(context.get(key) or "")
            if known:
                if known[0] in (AGENT, AI) and context.get("user_id"):
                    self.own_user_id = context["user_id"]
                return known
        if context.get("parent_id"):
            return AUTOMATION, ""
        user_id = context.get("user_id")
        if user_id:
            return (AI, "") if user_id == self.own_user_id else (RESIDENT, "")
        return DEVICE, ""

    @staticmethod
    def describe(origin: str, label: str) -> str:
        return f"{origin}: {label}" if label and origin == AUTOMATION else origin
