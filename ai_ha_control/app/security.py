"""Which entities guard the house. The agent never operates these by itself, whatever the settings say."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .ha.index import normalize

if TYPE_CHECKING:
    from .ha.index import EntityIndex, EntityInfo

SECURITY_DOMAINS = {"lock", "alarm_control_panel"}
SECURITY_COVER_CLASSES = {"garage", "gate", "door"}
# Domains where a door opener or alarm can hide behind a plain on/off (relays, Nuki Opener, scenes, scripts).
CONTROLLABLE_DOMAINS = {"switch", "button", "input_button", "cover", "script", "scene", "input_boolean", "siren"}
SECURITY_WORDS = ("deur", "poort", "garage", "slot", "alarm", "hek", "gate", "door", "lock", "opener",
                  "inbraak", "beveiliging", "security", "sirene", "siren")


NOT_SECURITY = {"outdoor", "indoor", "clock", "deurbel", "doorbell", "deurbelgong"}


def _has_security_word(text: str) -> bool:
    """Dutch compounds ("voordeur", "garagedeuropener") count; "outdoor" or "deurbel" do not."""
    for token in normalize(text).split():
        if token in NOT_SECURITY:
            continue
        if any(token.startswith(w) or token.endswith(w) for w in SECURITY_WORDS):
            return True
    return False


def _words_match(ent: EntityInfo) -> bool:
    return _has_security_word(f"{ent.entity_id.split('.', 1)[1].replace('_', ' ')} {ent.name} {ent.device or ''}")


def _security_devices(index: EntityIndex) -> set[str]:
    """Devices that carry a lock, an alarm panel or a garage/gate cover."""
    devices = set()
    for ent in index.entities.values():
        if not ent.device_id:
            continue
        if ent.domain in SECURITY_DOMAINS or (ent.domain == "cover" and ent.device_class in SECURITY_COVER_CLASSES):
            devices.add(ent.device_id)
    return devices


def is_security_entity(index: EntityIndex, entity_id: str, _devices: set[str] | None = None) -> bool:
    domain = entity_id.split(".", 1)[0]
    if domain in SECURITY_DOMAINS:
        return True
    ent = index.get(entity_id)
    if ent is None:
        return domain in CONTROLLABLE_DOMAINS and _has_security_word(entity_id.split(".", 1)[1].replace("_", " "))
    if domain == "cover" and ent.device_class in SECURITY_COVER_CLASSES:
        return True
    if domain not in CONTROLLABLE_DOMAINS:
        return False
    devices = _security_devices(index) if _devices is None else _devices
    return bool(ent.device_id and ent.device_id in devices) or _words_match(ent)


def security_entities(index: EntityIndex, entity_ids: list[str]) -> list[str]:
    devices = _security_devices(index)
    return [eid for eid in entity_ids if is_security_entity(index, eid, devices)]


def touches_security(index: EntityIndex, domain: str, entity_ids: list[str]) -> bool:
    """A service call on a door, lock, alarm or anything that could open or disarm them."""
    if domain in SECURITY_DOMAINS:
        return True
    return bool(security_entities(index, entity_ids))


def payload_touches_security(index: EntityIndex, kind: str, payload: dict[str, Any]) -> bool:
    """For confirmations: does this pending action (service call or new automation) touch security?"""
    if kind == "service":
        return touches_security(index, payload.get("domain", ""), payload.get("entity_ids") or [])
    text = json.dumps(payload, ensure_ascii=False)
    if any(f'"{d}.' in text or f"'{d}." in text for d in SECURITY_DOMAINS):
        return True
    devices = _security_devices(index)
    return any(eid in text and is_security_entity(index, eid, devices) for eid in index.entities)
