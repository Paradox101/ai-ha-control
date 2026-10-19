"""Decides whether the AI may call a Home Assistant service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from .config import Config
from .security import security_entities

if TYPE_CHECKING:
    from .ha.index import EntityIndex


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


# Covers/doors that guard the house; always need confirmation even if "cover" is allowed.
SENSITIVE_ENTITY_PATTERNS = ("cover.*garage*", "cover.*poort*", "cover.*gate*", "lock.*")


@dataclass
class PolicyResult:
    decision: Decision
    reason: str = ""


class Policy:
    def __init__(self, config: Config, index: EntityIndex | None = None) -> None:
        self._config = config
        self._index = index

    def check(self, domain: str, service: str, entity_ids: list[str]) -> PolicyResult:
        full = f"{domain}.{service}"
        if domain in self._config.deny_domains:
            return PolicyResult(Decision.DENY, f"domein '{domain}' is niet toegestaan")
        if full in self._config.deny_services:
            return PolicyResult(Decision.DENY, f"service '{full}' is niet toegestaan")

        target_domains = {eid.split(".", 1)[0] for eid in entity_ids}
        blocked = target_domains & set(self._config.deny_domains)
        if blocked:
            return PolicyResult(Decision.DENY, f"entiteiten in domein {sorted(blocked)} zijn niet toegestaan")

        if domain in self._config.confirm_domains or target_domains & set(self._config.confirm_domains):
            return PolicyResult(Decision.CONFIRM, "deze actie vereist bevestiging")
        for eid in entity_ids:
            if any(fnmatch(eid, pattern) for pattern in SENSITIVE_ENTITY_PATTERNS):
                return PolicyResult(Decision.CONFIRM, f"{eid} is beveiligingsgevoelig")
        sensitive = security_entities(self._index, entity_ids) if self._index is not None else []
        if sensitive:
            return PolicyResult(Decision.CONFIRM, f"{sensitive[0]} is beveiligingsgevoelig")
        if not entity_ids and domain not in ("notify", "persistent_notification"):
            return PolicyResult(Decision.CONFIRM, "actie zonder specifieke entiteit")
        return PolicyResult(Decision.ALLOW)
