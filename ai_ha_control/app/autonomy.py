"""What the autonomous agent may do by itself, and undoing what it did."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from .security import security_entities

if TYPE_CHECKING:
    from .config import Config
    from .ha.index import EntityIndex
    from .storage.db import Database
    from .tools.base import ToolContext

_LOGGER = logging.getLogger(__name__)

OBSERVE, SHADOW, ASK, ACT = "observe", "shadow", "ask", "act"
# shadow: decide as if acting, but only record what would have been done (to judge the agent first).
LEVEL_ORDER = {OBSERVE: 0, SHADOW: 1, ASK: 2, ACT: 3}
DEFAULT_LEVEL = ASK
# Doors, locks and the alarm: never by the agent itself, whatever the settings say.
MAX_LEVEL = {"lock": ASK, "alarm_control_panel": ASK}
# Scenes and scripts can do anything (arm the alarm, open the gate): only the ones the user released.
INDIRECT_DOMAINS = {"scene", "script", "automation"}

# Only actions that can be restored from a snapshot of the previous state.
REVERSIBLE_SERVICES = {
    "turn_on", "turn_off", "toggle", "set_temperature", "set_hvac_mode", "set_preset_mode", "set_percentage",
    "set_fan_mode", "set_cover_position", "open_cover", "close_cover", "volume_set", "media_pause", "media_play",
}
SNAPSHOT_ATTRIBUTES = ("brightness", "color_temp_kelvin", "temperature", "hvac_modes", "preset_mode",
                       "percentage", "current_position", "volume_level")


def parse_levels(items: list[str]) -> dict[str, str]:
    levels = {}
    for item in items:
        domain, _, level = item.partition("=")
        if level.strip() in LEVEL_ORDER:
            levels[domain.strip()] = level.strip()
    return levels


def clamp(domain: str, level: str) -> str:
    cap = MAX_LEVEL.get(domain)
    return cap if cap and LEVEL_ORDER[level] > LEVEL_ORDER[cap] else level


class Autonomy:
    def __init__(self, config: Config, index: EntityIndex, db: Database) -> None:
        self._config = config
        self._index = index
        self._db = db

    def level(self, domain: str) -> str:
        return clamp(domain, parse_levels(self._config.autonomy).get(domain, DEFAULT_LEVEL))

    @property
    def enabled(self) -> bool:
        """Acting on its own requires the option and, if configured, the kill switch to be on."""
        if not self._config.autonomous:
            return False
        if not self._config.kill_switch:
            return True
        switch = self._index.get(self._config.kill_switch)
        return switch is not None and switch.state == "on"

    def summary(self) -> str:
        levels = parse_levels(self._config.autonomy)
        text = ", ".join(f"{d}={clamp(d, lvl)}" for d, lvl in sorted(levels.items()))
        return f"{text}; overige domeinen={DEFAULT_LEVEL}; zelf handelen {'AAN' if self.enabled else 'UIT'}"

    def decide(self, domain: str, service: str, entity_ids: list[str], data: dict[str, Any],
               planned: bool = False) -> tuple[str, str]:
        """Return (level, reason) for an action the agent wants to take.

        planned: the action comes from a comfort profile or playbook the user set up (not from the LLM),
        so the limit on the size of a temperature change does not apply.
        """
        domains = {domain} | {e.split(".", 1)[0] for e in entity_ids}
        level = min((self.level(d) for d in domains), key=LEVEL_ORDER.__getitem__)
        if level == OBSERVE:
            return level, f"alleen observeren voor {sorted(domains)}"
        if not entity_ids:
            return ASK, "actie zonder specifieke entiteit"
        sensitive = security_entities(self._index, entity_ids)
        if sensitive or domain in MAX_LEVEL:
            what = sensitive[0] if sensitive else domain
            return ASK, f"{what} raakt deuren of alarm: dat doet de agent nooit zelf"
        indirect = [e for e in entity_ids if e.split(".", 1)[0] in INDIRECT_DOMAINS or domain in INDIRECT_DOMAINS]
        released = set(self._config.autonomous_scenes)
        if any(e not in released for e in indirect):
            return ASK, "scènes en scripts alleen zelf starten als ze zijn vrijgegeven"
        if level == ASK:
            return ASK, ""
        overridden = [e for e in entity_ids if e in self._db.active_overrides()]
        if overridden:
            return ASK, f"de bewoner heeft {overridden[0]} kort geleden zelf aangepast"
        if level == SHADOW:
            return SHADOW, "schaduwmodus"
        if not self.enabled:
            return ASK, "zelfstandig handelen staat uit (noodknop)"
        if service not in REVERSIBLE_SERVICES:
            return ASK, f"{domain}.{service} is niet automatisch terug te draaien"
        if self._db.count_actions(time.time() - 3600) >= self._config.max_autonomous_actions_per_hour:
            return ASK, "maximum aantal eigen acties per uur bereikt"
        if domain == "climate" and "temperature" in data and not planned:
            for eid in entity_ids:
                ent = self._index.get(eid)
                current = ent.attributes.get("temperature") if ent else None
                if current is not None and abs(float(data["temperature"]) - float(current)) > \
                        self._config.max_climate_change:
                    return ASK, f"temperatuurwijziging groter dan {self._config.max_climate_change} graden"
        return ACT, ""

    def snapshot(self, entity_ids: list[str]) -> dict[str, Any]:
        previous = {}
        for eid in entity_ids:
            ent = self._index.get(eid)
            if ent:
                previous[eid] = {"state": ent.state,
                                 "attributes": {k: ent.attributes[k] for k in SNAPSHOT_ATTRIBUTES
                                                if ent.attributes.get(k) is not None}}
        return previous


def restore_calls(entity_id: str, previous: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Service calls that bring an entity back to a previously recorded state."""
    domain = entity_id.split(".", 1)[0]
    state, attrs = previous.get("state"), previous.get("attributes", {})
    if domain == "climate":
        calls = []
        if state and state in (attrs.get("hvac_modes") or [state]):
            calls.append(("climate", "set_hvac_mode", {"hvac_mode": state}))
        if attrs.get("temperature") is not None:
            calls.append(("climate", "set_temperature", {"temperature": attrs["temperature"]}))
        return calls
    if domain == "cover":
        if attrs.get("current_position") is not None:
            return [("cover", "set_cover_position", {"position": attrs["current_position"]})]
        return [("cover", "open_cover" if state == "open" else "close_cover", {})]
    if state == "off":
        return [(domain, "turn_off", {})]
    data: dict[str, Any] = {}
    if domain == "light":
        if attrs.get("brightness") is not None:
            data["brightness"] = attrs["brightness"]
        if attrs.get("color_temp_kelvin") is not None:
            data["color_temp_kelvin"] = attrs["color_temp_kelvin"]
    if domain == "fan" and attrs.get("percentage") is not None:
        data["percentage"] = attrs["percentage"]
    if domain == "media_player" and attrs.get("volume_level") is not None and state in ("playing", "paused", "on"):
        return [("media_player", "volume_set", {"volume_level": attrs["volume_level"]})]
    return [(domain, "turn_on", data)]


async def undo_action(ctx: ToolContext, action_id: int) -> dict[str, Any]:
    action = ctx.db.get_action(action_id)
    if action is None:
        return {"status": "error", "error": "actie niet gevonden"}
    if action["undone"]:
        return {"status": "error", "error": "al ongedaan gemaakt"}
    calls = [(eid, call) for eid, prev in action["previous"].items() for call in restore_calls(eid, prev)]
    if ctx.config.dry_run:
        ctx.db.audit("undo", {"action_id": action_id}, "dry_run", None, ctx.conversation_id)
        return {"status": "dry_run", "message": "Testmodus: niet teruggedraaid."}
    try:
        for eid, (domain, service, data) in calls:
            await ctx.ha.call_service(domain, service, data, {"entity_id": [eid]})
    except Exception as err:  # noqa: BLE001
        ctx.db.audit("undo", {"action_id": action_id}, "failed", str(err), ctx.conversation_id)
        return {"status": "error", "error": str(err)}
    ctx.db.mark_undone(action_id)
    ctx.db.audit("undo", {"action_id": action_id, "calls": len(calls)}, "executed", "ok", ctx.conversation_id)
    return {"status": "ok", "description": f"{action['domain']}.{action['service']} teruggedraaid"}
