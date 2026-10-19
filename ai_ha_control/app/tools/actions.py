"""Executing Home Assistant services under the policy, with confirmations."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..policy import Decision

if TYPE_CHECKING:
    from .base import ToolContext

_LOGGER = logging.getLogger(__name__)

CONFIRMATION_TTL = 1800  # seconds


@dataclass
class PendingAction:
    id: str
    kind: str  # "service" | "automation"
    description: str
    reason: str
    payload: dict[str, Any]
    conversation_id: str | None
    created: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "reason": self.reason,
            "created": self.created,
            "conversation_id": self.conversation_id,
        }


class ConfirmationStore:
    def __init__(self) -> None:
        self._pending: dict[str, PendingAction] = {}

    def add(self, action: PendingAction) -> None:
        self._purge()
        self._pending[action.id] = action

    def pop(self, action_id: str) -> PendingAction | None:
        self._purge()
        return self._pending.pop(action_id, None)

    def list(self, conversation_id: str | None = None) -> list[PendingAction]:
        self._purge()
        actions = list(self._pending.values())
        if conversation_id is not None:
            actions = [a for a in actions if a.conversation_id == conversation_id]
        return actions

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, a in self._pending.items() if now - a.created > CONFIRMATION_TTL]:
            del self._pending[key]


def describe_service(domain: str, service: str, entity_ids: list[str], data: dict[str, Any]) -> str:
    targets = ", ".join(entity_ids) or "-"
    extra = f" {data}" if data else ""
    return f"{domain}.{service} → {targets}{extra}"


async def queue_confirmation(
    ctx: ToolContext, kind: str, payload: dict[str, Any], description: str, reason: str
) -> dict[str, Any]:
    pending = PendingAction(
        id=uuid.uuid4().hex[:8],
        kind=kind,
        description=description,
        reason=reason,
        payload=payload,
        conversation_id=ctx.conversation_id,
    )
    ctx.confirmations.add(pending)
    ctx.db.audit(kind, payload, "pending", reason, ctx.conversation_id)
    where = "in de web-UI"
    if ctx.source != "web" and ctx.notifier is not None:
        await ctx.notifier.ask_confirmation(pending)
        where = "op de telefoon of door 'ja' te zeggen"
    return {
        "status": "needs_confirmation",
        "confirmation_id": pending.id,
        "message": f"Nog niet uitgevoerd: {reason}. De gebruiker moet dit bevestigen {where}.",
    }


async def resolve_confirmation(ctx: ToolContext, action_id: str, approve: bool) -> dict[str, Any]:
    """Execute or reject a pending action (from the UI, a phone notification or voice)."""
    pending = ctx.confirmations.pop(action_id)
    if pending is None:
        return {"status": "error", "error": "onbekende of verlopen bevestiging"}
    if not approve:
        ctx.db.audit(pending.kind, pending.payload, "rejected", None, pending.conversation_id)
        return {"status": "rejected", "description": pending.description}
    if pending.kind == "automation":
        from .automations import save_automation

        result = await save_automation(ctx, pending.payload["config"], confirmed=True)
    else:
        p = pending.payload
        result = await execute(ctx, p["domain"], p["service"], p["entity_ids"], p["data"], confirmed=True)
    if result.get("status") == "error":
        ctx.confirmations.add(pending)  # keep it so the user can try again
    return {**result, "description": pending.description}


def _validate(ctx: ToolContext, domain: str, service: str, entity_ids: list[str]) -> str | None:
    if ctx.index.services and service not in ctx.index.services.get(domain, {}):
        known = sorted(ctx.index.services.get(domain, {}))[:20]
        if known:
            return f"service {domain}.{service} bestaat niet. Beschikbaar: {known}"
        return f"domein '{domain}' heeft geen services"
    for eid in entity_ids:
        if ctx.index.get(eid) is None:
            guesses = [e.entity_id for e in ctx.index.search(eid.replace(".", " ").replace("_", " "), limit=5)]
            return f"entiteit '{eid}' bestaat niet. Bedoelde je: {guesses}? Gebruik search_entities."
    return None


async def _states_after(ctx: ToolContext, entity_ids: list[str]) -> list[str]:
    await asyncio.sleep(1.0)
    return [ctx.index.get(eid).compact(with_attributes=True) for eid in entity_ids if ctx.index.get(eid)]


async def perform(
    ctx: ToolContext,
    domain: str,
    service: str,
    entity_ids: list[str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate, check policy, and either execute, dry-run or queue for confirmation."""
    data = {k: v for k, v in (data or {}).items() if v is not None and k != "entity_id"}
    args = {"domain": domain, "service": service, "entity_ids": entity_ids, "data": data}

    error = _validate(ctx, domain, service, entity_ids)
    if error:
        return {"status": "error", "error": error}

    result = ctx.policy.check(domain, service, entity_ids)
    if result.decision == Decision.DENY:
        ctx.db.audit("service", args, "denied", result.reason, ctx.conversation_id)
        return {"status": "denied", "reason": result.reason}
    if result.decision == Decision.CONFIRM:
        return await queue_confirmation(
            ctx, "service", args, describe_service(domain, service, entity_ids, data), result.reason
        )
    return await execute(ctx, domain, service, entity_ids, data)


async def execute(
    ctx: ToolContext,
    domain: str,
    service: str,
    entity_ids: list[str],
    data: dict[str, Any],
    confirmed: bool = False,
) -> dict[str, Any]:
    args = {"domain": domain, "service": service, "entity_ids": entity_ids, "data": data}
    if ctx.config.dry_run:
        ctx.db.audit("service", args, "dry_run", None, ctx.conversation_id)
        return {
            "status": "dry_run",
            "message": f"Testmodus: {domain}.{service} op {entity_ids} is NIET echt uitgevoerd.",
        }
    target = {"entity_id": entity_ids} if entity_ids else None
    try:
        await ctx.ha.call_service(domain, service, data, target)
    except Exception as err:  # noqa: BLE001
        ctx.db.audit("service", args, "failed", str(err), ctx.conversation_id)
        return {"status": "error", "error": str(err)}
    ctx.db.audit("service", args, "confirmed" if confirmed else "executed", "ok", ctx.conversation_id)
    return {"status": "ok", "new_states": await _states_after(ctx, entity_ids)}
