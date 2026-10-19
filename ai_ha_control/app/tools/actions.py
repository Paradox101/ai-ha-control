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

CONFIRMATION_TTL = 600  # seconds


@dataclass
class PendingAction:
    id: str
    domain: str
    service: str
    entity_ids: list[str]
    data: dict[str, Any]
    reason: str
    conversation_id: str | None
    created: float = field(default_factory=time.time)

    @property
    def description(self) -> str:
        targets = ", ".join(self.entity_ids) or "-"
        extra = f" {self.data}" if self.data else ""
        return f"{self.domain}.{self.service} → {targets}{extra}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
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

    def list(self) -> list[PendingAction]:
        self._purge()
        return list(self._pending.values())

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, a in self._pending.items() if now - a.created > CONFIRMATION_TTL]:
            del self._pending[key]


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
        ctx.db.audit("call_service", args, "denied", result.reason, ctx.conversation_id)
        return {"status": "denied", "reason": result.reason}

    if result.decision == Decision.CONFIRM:
        pending = PendingAction(
            id=uuid.uuid4().hex[:8],
            domain=domain,
            service=service,
            entity_ids=entity_ids,
            data=data,
            reason=result.reason,
            conversation_id=ctx.conversation_id,
        )
        ctx.confirmations.add(pending)
        ctx.db.audit("call_service", args, "pending", result.reason, ctx.conversation_id)
        return {
            "status": "needs_confirmation",
            "confirmation_id": pending.id,
            "message": f"Niet uitgevoerd: {result.reason}. De gebruiker moet dit bevestigen in de web-UI.",
        }

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
        ctx.db.audit("call_service", args, "dry_run", None, ctx.conversation_id)
        return {
            "status": "dry_run",
            "message": f"Testmodus: {domain}.{service} op {entity_ids} is NIET echt uitgevoerd.",
        }
    target = {"entity_id": entity_ids} if entity_ids else None
    try:
        await ctx.ha.call_service(domain, service, data, target)
    except Exception as err:  # noqa: BLE001
        ctx.db.audit("call_service", args, "failed", str(err), ctx.conversation_id)
        return {"status": "error", "error": str(err)}
    ctx.db.audit("call_service", args, "confirmed" if confirmed else "executed", "ok", ctx.conversation_id)
    return {"status": "ok", "new_states": await _states_after(ctx, entity_ids)}
