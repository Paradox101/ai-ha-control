"""Watch rules: "warn when the freezer gets above -15" checked by code on every change, not by the LLM."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..ha.index import EntityInfo
from ..tools.actions import queue_confirmation
from ..tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from .incidents import IncidentManager

_LOGGER = logging.getLogger(__name__)

OPERATORS = (">", "<", "=", "!=")
OPERATOR_TEXT = {">": "boven", "<": "onder", "=": "gelijk aan", "!=": "niet"}
SEVERITIES = ("info", "warning", "critical")
MODES = ("thuis", "weg", "slapen", "vakantie", "gast")
IGNORED_STATES = ("unavailable", "unknown", "")


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def matches(state: str, op: str, value: str) -> bool | None:
    """Does the state break the rule? None when it can't be judged (unavailable, not a number)."""
    if state in IGNORED_STATES:
        return None
    current, limit = _number(state), _number(value)
    if op in (">", "<"):
        if current is None or limit is None:
            return None
        return current > limit if op == ">" else current < limit
    if current is not None and limit is not None:
        equal = current == limit
    else:
        equal = state.strip().lower() == str(value).strip().lower()
    return equal if op == "=" else not equal


def describe(rule: dict[str, Any], ent: EntityInfo | None) -> str:
    name = ent.name if ent else rule["entity_id"]
    unit = (ent.attributes.get("unit_of_measurement") or "") if ent else ""
    text = f"{name} {OPERATOR_TEXT.get(rule['op'], rule['op'])} {rule['value']}{(' ' + unit) if unit else ''}"
    if rule.get("minutes"):
        text += f", langer dan {rule['minutes']} min"
    if rule.get("modes"):
        text += f" (alleen bij {', '.join(rule['modes'])})"
    return text


class ThresholdMonitor:
    def __init__(self, ctx: ToolContext, incidents: IncidentManager) -> None:
        self._ctx = ctx
        self._incidents = incidents
        self.mode: str | None = None  # house mode, set by the scheduler; None = don't filter

    async def check(self, ent: EntityInfo, now: float | None = None) -> None:
        for rule in self._ctx.db.watch_rules(enabled_only=True, entity_id=ent.entity_id):
            await self._evaluate(rule, ent, now or time.time())

    async def tick(self, mode: str | None = None, now: float | None = None) -> None:
        """Every minute: rules with a duration fire once the condition has lasted long enough."""
        if mode is not None:
            self.mode = mode
        now = now or time.time()
        for rule in self._ctx.db.watch_rules(enabled_only=True):
            ent = self._ctx.index.get(rule["entity_id"])
            if ent is not None:
                await self._evaluate(rule, ent, now)

    async def _evaluate(self, rule: dict[str, Any], ent: EntityInfo, now: float) -> None:
        db = self._ctx.db
        broken = matches(ent.state, rule["op"], rule["value"])
        if broken is None:
            return
        if not broken:
            if rule["since"] is not None or rule["alerted"]:
                db.set_watch_state(rule["id"], None, False)
            if rule["alerted"]:
                await self._recovered(rule, ent)
            return
        since = rule["since"] if rule["since"] is not None else now
        if rule["since"] is None:
            db.set_watch_state(rule["id"], since, False)
        if rule["alerted"] or now - since < (rule["minutes"] or 0) * 60:
            return
        if rule["modes"] and self.mode is not None and self.mode not in rule["modes"]:
            return
        db.set_watch_state(rule["id"], since, True)
        unit = ent.attributes.get("unit_of_measurement") or ""
        lasted = f" (al {int((now - since) / 60)} min)" if rule["minutes"] else ""
        text = f"{ent.name} is {ent.state}{(' ' + unit) if unit else ''}{lasted}. Regel: {describe(rule, ent)}."
        await self._incidents.report(
            f"watch:{rule['id']}", rule.get("label") or f"{ent.name}: {OPERATOR_TEXT[rule['op']]} {rule['value']}",
            text, severity=rule["severity"], entity_id=ent.entity_id, correlate_seconds=86400,
            critical=rule["severity"] == "critical", respect_quiet_hours=rule["severity"] != "info")

    async def _recovered(self, rule: dict[str, Any], ent: EntityInfo) -> None:
        unit = ent.attributes.get("unit_of_measurement") or ""
        text = f"{ent.name} is weer normaal: {ent.state}{(' ' + unit) if unit else ''}."
        incident = self._incidents.find(kind=f"watch:{rule['id']}")
        if incident is not None:
            self._incidents.resolve(incident["id"], text)
            self._ctx.db.ack_events(incident_id=incident["id"])
        if self._ctx.notifier is not None:
            await self._ctx.notifier.send("✅ Weer normaal", text, kind="watch", entity_id=ent.entity_id,
                                          incident_id=incident["id"] if incident else None)


def validate(ctx: ToolContext, payload: dict[str, Any]) -> str | None:
    if ctx.index.get(payload["entity_id"]) is None:
        return f"entiteit '{payload['entity_id']}' bestaat niet; gebruik search_entities"
    if payload["op"] not in OPERATORS:
        return f"operator moet een van {', '.join(OPERATORS)} zijn"
    if payload["op"] in (">", "<") and _number(payload["value"]) is None:
        return "boven/onder werkt alleen met een getal"
    if payload["severity"] not in SEVERITIES:
        return f"ernst moet een van {', '.join(SEVERITIES)} zijn"
    unknown = [m for m in payload["modes"] if m not in MODES]
    if unknown:
        return f"onbekende huismodus: {', '.join(unknown)} (kies uit {', '.join(MODES)})"
    return None


def normalize(entity_id: str, operator: str, value: Any, minutes: Any = 0, modes: Any = None,
              severity: str = "warning", label: str = "") -> dict[str, Any]:
    modes = [modes] if isinstance(modes, str) else list(modes or [])
    return {"entity_id": str(entity_id).strip(), "op": str(operator).strip().replace("==", "="),
            "value": str(value).strip(), "minutes": max(0, int(_number(minutes) or 0)),
            "modes": [str(m).strip().lower() for m in modes if str(m).strip()],
            "severity": (severity or "warning").strip().lower(), "label": (label or "").strip()[:80]}


def save_watch_rule(ctx: ToolContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Store a rule in the shape normalize() returns (after confirmation, or from the web UI)."""
    error = validate(ctx, payload)
    if error:
        return {"status": "error", "error": error}
    return {"status": "ok", "rule_id": ctx.db.add_watch_rule(payload)}


async def add_watch_rule(ctx: ToolContext, entity_id: str, operator: str, value: Any, minutes: int = 0,
                         modes: Any = None, severity: str = "warning", label: str = "") -> dict[str, Any]:
    payload = normalize(entity_id, operator, value, minutes, modes, severity, label)
    error = validate(ctx, payload)
    if error:
        return {"status": "error", "error": error}
    text = describe(payload, ctx.index.get(payload["entity_id"]))
    return await queue_confirmation(ctx, "watch", payload, f"Bewakingsregel: waarschuw als {text}",
                                    "een bewakingsregel stuurt meldingen zonder tussenkomst van de AI")


async def list_watch_rules(ctx: ToolContext) -> dict[str, Any]:
    return {"rules": [f"{r['id']}: {describe(r, ctx.index.get(r['entity_id']))}" + ("" if r["enabled"] else " (uit)")
                      for r in ctx.db.watch_rules()]}


async def remove_watch_rule(ctx: ToolContext, rule_id: int) -> dict[str, Any]:
    return {"status": "ok" if ctx.db.delete_watch_rule(int(rule_id)) else "not_found"}


WATCH_TOOLS = [
    Tool(
        "add_watch_rule",
        "Maak een bewakingsregel voor een meetwaarde of status: de add-on waarschuwt zelf, direct, zonder AI. "
        "Gebruik dit voor 'waarschuw als de vriezer boven -15 graden komt' (sensor, >, -15) of 'meld als de "
        "achterdeur langer dan 10 minuten open staat' (binary_sensor, =, on, minutes=10). Zoek eerst de "
        "entiteit met search_entities. modes: alleen in deze huismodi (thuis, weg, slapen, vakantie, gast). "
        "severity: info, warning of critical. De gebruiker moet de regel bevestigen.",
        {"type": "object", "properties": {
            "entity_id": {"type": "string"},
            "operator": {"type": "string", "enum": list(OPERATORS)},
            "value": {"type": "string"},
            "minutes": {"type": "integer", "description": "zo lang moet het zo zijn voor een melding"},
            "modes": {"type": "array", "items": {"type": "string", "enum": list(MODES)}},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "label": {"type": "string", "description": "korte titel voor de melding"}},
         "required": ["entity_id", "operator", "value"]},
        add_watch_rule,
    ),
    Tool("list_watch_rules", "Toon de bewakingsregels.", {"type": "object", "properties": {}}, list_watch_rules),
    Tool(
        "remove_watch_rule",
        "Verwijder een bewakingsregel.",
        {"type": "object", "properties": {"rule_id": {"type": "integer"}}, "required": ["rule_id"]},
        remove_watch_rule,
    ),
]
