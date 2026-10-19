"""Executing Home Assistant services under the policy, with confirmations."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..policy import Decision
from ..security import TARGET_KEYS, payload_touches_security

if TYPE_CHECKING:
    from ..storage.db import Database
    from .base import ToolContext

_LOGGER = logging.getLogger(__name__)

CONFIRMATION_TTL = 1800  # seconds
VERIFY_DELAY = 45.0      # seconds before checking that a device really followed an autonomous action
_background: set[asyncio.Task] = set()


@dataclass
class PendingAction:
    id: str
    kind: str  # "service" | "automation"
    description: str
    reason: str
    payload: dict[str, Any]
    conversation_id: str | None
    created: float = field(default_factory=time.time)
    sensitive: bool = False  # touches doors/alarm: only confirmable on the phone or in the web UI, not by voice

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "reason": self.reason,
            "created": self.created,
            "conversation_id": self.conversation_id,
            "sensitive": self.sensitive,
        }


class ConfirmationStore:
    """Pending confirmations; kept in the database so phone buttons still work after a restart."""

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._pending: dict[str, PendingAction] = {}
        if db is not None:
            for row in db.pending_confirmations():
                self._pending[row["id"]] = PendingAction(**row)

    def add(self, action: PendingAction) -> None:
        self._purge()
        self._pending[action.id] = action
        if self._db is not None:
            self._db.save_confirmation(action.as_dict() | {"payload": action.payload})

    def pop(self, action_id: str) -> PendingAction | None:
        self._purge()
        action = self._pending.pop(action_id, None)
        if action is not None and self._db is not None:
            self._db.delete_confirmation(action_id)
        return action

    def list(self, conversation_id: str | None = None) -> list[PendingAction]:
        self._purge()
        actions = list(self._pending.values())
        if conversation_id is not None:
            actions = [a for a in actions if a.conversation_id == conversation_id]
        return actions

    def drop_vehicle_locks(self) -> int:
        """Up to 0.6.23 a car's lock was asked about like a house door: drop those questions."""
        from ..ha.index import EntityInfo
        from ..house import is_car

        def car(entity_id: str) -> bool:
            return is_car(EntityInfo(entity_id=entity_id, name="", domain=entity_id.split(".", 1)[0]))

        stale = [k for k, a in self._pending.items()
                 if a.payload.get("domain") == "lock" and any(car(e) for e in a.payload.get("entity_ids") or [])]
        for key in stale:
            self.pop(key)
        return len(stale)

    def _purge(self) -> None:
        now = time.time()
        for key in [k for k, a in self._pending.items() if now - a.created > CONFIRMATION_TTL]:
            del self._pending[key]
            if self._db is not None:
                self._db.delete_confirmation(key)


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
        sensitive=payload_touches_security(ctx.index, kind, payload),
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


async def resolve_confirmation(ctx: ToolContext, action_id: str, approve: bool, via: str = "ui") -> dict[str, Any]:
    """Execute or reject a pending action (from the UI, a phone notification or voice)."""
    pending = ctx.confirmations.pop(action_id)
    if pending is None:
        return {"status": "error", "error": "onbekende of verlopen bevestiging"}
    if approve and via == "voice" and pending.sensitive:
        # Anyone near a speaker (or the tv) can say "ja": doors and the alarm need the phone or the web UI.
        ctx.confirmations.add(pending)
        ctx.db.audit(pending.kind, pending.payload, "voice_refused", "beveiligingsgevoelig", pending.conversation_id)
        return {"status": "error", "description": pending.description,
                "error": "dit raakt deuren of het alarm; bevestig het op je telefoon of in de web-UI"}
    if not approve:
        ctx.db.audit(pending.kind, pending.payload, "rejected", None, pending.conversation_id)
        return {"status": "rejected", "description": pending.description}
    if pending.kind == "automation":
        from .automations import save_automation

        result = await save_automation(ctx, pending.payload["config"], confirmed=True)
    elif pending.kind == "profile":
        from ..comfort import save_profile

        result = save_profile(ctx.db, pending.payload)
    elif pending.kind == "watch":
        from ..watcher.thresholds import save_watch_rule

        result = save_watch_rule(ctx, pending.payload)
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
    planned: bool = False,
) -> dict[str, Any]:
    """Validate, check policy, and either execute, dry-run or queue for confirmation."""
    data = {k: v for k, v in (data or {}).items() if v is not None and k != "entity_id"}
    args = {"domain": domain, "service": service, "entity_ids": entity_ids, "data": data}
    # Targets hidden in data would skip the policy and the door/alarm checks, which only see entity_ids.
    hidden = [k for k in (*TARGET_KEYS, "entities", "snapshot_entities") if k in data]
    if hidden:
        return {"status": "error",
                "error": f"{', '.join(hidden)} in data is niet toegestaan; noem de entiteiten in entity_ids"}

    if ctx.source == "autonomous" and ctx.config.kill_switch in entity_ids:
        return {"status": "denied", "reason": "de agent mag zijn eigen noodknop niet bedienen"}
    error = _validate(ctx, domain, service, entity_ids)
    if error:
        return {"status": "error", "error": error}

    result = ctx.policy.check(domain, service, entity_ids)
    if result.decision == Decision.DENY:
        ctx.db.audit("service", args, "denied", result.reason, ctx.conversation_id)
        return {"status": "denied", "reason": result.reason}
    description = describe_service(domain, service, entity_ids, data)
    if ctx.source == "autonomous" and ctx.autonomy is not None:
        return await _perform_autonomous(ctx, domain, service, entity_ids, data, result.decision, result.reason,
                                         planned)
    if result.decision == Decision.CONFIRM:
        return await queue_confirmation(ctx, "service", args, description, result.reason)
    return await execute(ctx, domain, service, entity_ids, data)


async def _perform_autonomous(
    ctx: ToolContext,
    domain: str,
    service: str,
    entity_ids: list[str],
    data: dict[str, Any],
    policy_decision: Decision,
    policy_reason: str,
    planned: bool = False,
) -> dict[str, Any]:
    """The agent acts on its own only where its autonomy level allows it."""
    args = {"domain": domain, "service": service, "entity_ids": entity_ids, "data": data}
    description = describe_service(domain, service, entity_ids, data)
    level, reason = ctx.autonomy.decide(domain, service, entity_ids, data, planned=planned)
    if level == "observe":
        ctx.db.audit("service", args, "observe_only", reason, ctx.conversation_id)
        return {"status": "not_allowed", "reason": f"{reason}; meld het de gebruiker in plaats van te handelen"}
    if level == "shadow" and policy_decision != Decision.CONFIRM:
        action_id = ctx.db.add_action(domain, service, entity_ids, data, ctx.autonomy.snapshot(entity_ids),
                                      ctx.reason, shadow=True)
        ctx.db.audit("service", args, "shadow", ctx.reason, ctx.conversation_id)
        return {"status": "shadow", "action_id": action_id,
                "message": "Schaduwmodus: niet uitgevoerd, wel vastgelegd zodat de bewoner het kan beoordelen."}
    if level in ("ask", "shadow") or policy_decision == Decision.CONFIRM:
        return await queue_confirmation(ctx, "service", args, description,
                                        policy_reason or reason or "de agent vraagt toestemming")
    previous = ctx.autonomy.snapshot(entity_ids)
    outcome = await execute(ctx, domain, service, entity_ids, data)
    if outcome.get("status") == "ok":
        action_id = ctx.db.add_action(domain, service, entity_ids, data, previous, ctx.reason)
        outcome["action_id"] = action_id
        if ctx.notifier is not None:
            await ctx.notifier.action_done(action_id, description, ctx.reason)
        task = asyncio.create_task(verify_action(ctx, action_id, domain, service, entity_ids, data))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return outcome


def followed(ent: Any, domain: str, service: str, data: dict[str, Any]) -> bool | None:
    """Did the entity reach the state the service asked for? None when we cannot tell."""
    if ent is None or ent.state in ("unavailable", "unknown"):
        return False
    if service == "turn_on" and domain not in ("scene", "script", "automation", "button"):
        return ent.state not in ("off",)
    if service == "turn_off":
        return ent.state in ("off", "idle", "standby")
    if service == "set_temperature" and "temperature" in data:
        target = ent.attributes.get("temperature")
        return target is not None and abs(float(target) - float(data["temperature"])) < 0.25
    if service == "set_hvac_mode":
        return ent.state == data.get("hvac_mode")
    if service == "open_cover":
        return ent.state in ("open", "opening")
    if service == "close_cover":
        return ent.state in ("closed", "closing")
    if service == "set_cover_position" and "position" in data:
        pos = ent.attributes.get("current_position")
        return pos is not None and abs(int(pos) - int(data["position"])) <= 5
    return None


async def verify_action(ctx: ToolContext, action_id: int, domain: str, service: str, entity_ids: list[str],
                        data: dict[str, Any], delay: float | None = None) -> list[str]:
    """Check the devices really followed; retry once, then open an incident. Returns entities that failed."""
    delay = VERIFY_DELAY if delay is None else delay
    if ctx.config.dry_run:
        return []
    failed = entity_ids
    for attempt in range(2):
        await asyncio.sleep(delay)
        failed = [e for e in failed if followed(ctx.index.get(e), domain, service, data) is False]
        if not failed:
            ctx.db.set_action_verified(action_id, "ok")
            return []
        if attempt == 0:
            # The resident may have reversed it on purpose, or pulled the kill switch: don't fight them.
            overrides = ctx.db.active_overrides()
            retry = [e for e in failed if e not in overrides]
            if not retry or (ctx.autonomy is not None and not ctx.autonomy.enabled):
                ctx.db.set_action_verified(action_id, "overridden")
                return []
            try:
                response = await ctx.ha.call_service(domain, service, data, {"entity_id": retry})
                if ctx.contexts is not None and isinstance(response, dict):
                    ctx.contexts.remember((response.get("context") or {}).get("id"), "agent", ctx.reason or "")
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Retry of %s.%s failed: %s", domain, service, err)
    ctx.db.set_action_verified(action_id, "failed")
    names = ", ".join(ctx.index.get(e).name if ctx.index.get(e) else e for e in failed)
    text = f"{names} reageert niet op {domain}.{service} (ook niet na een tweede poging)."
    if ctx.incidents is not None:
        await ctx.incidents.report(f"device:{failed[0]}", "Apparaat reageert niet", text, severity="info",
                                   entity_id=failed[0])
    return failed


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
        response = await ctx.ha.call_service(domain, service, data, target)
    except Exception as err:  # noqa: BLE001
        ctx.db.audit("service", args, "failed", str(err), ctx.conversation_id)
        return {"status": "error", "error": str(err)}
    if ctx.contexts is not None and isinstance(response, dict):
        # Remember the context of our own call: the state changes it causes are then recognised as ours.
        ctx.contexts.remember((response.get("context") or {}).get("id"), "agent" if ctx.source == "autonomous"
                              else "ai", ctx.reason or ctx.source)
    ctx.db.audit("service", args, "confirmed" if confirmed else "executed", "ok", ctx.conversation_id)
    return {"status": "ok", "new_states": await _states_after(ctx, entity_ids)}
