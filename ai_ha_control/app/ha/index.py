"""Compact, searchable index of the house: areas, devices, entities and states.

A small local model cannot see thousands of entities at once, so the agent first
searches this index and then asks for details of only the relevant entities.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Attributes worth showing to the model per domain (everything else is noise).
USEFUL_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "light": ("brightness", "color_temp_kelvin", "rgb_color", "color_mode"),
    "climate": (
        "current_temperature",
        "temperature",
        "target_temp_low",
        "target_temp_high",
        "hvac_action",
        "hvac_modes",
        "preset_mode",
        "preset_modes",
        "fan_mode",
        "current_humidity",
        "min_temp",
        "max_temp",
    ),
    "cover": ("current_position", "current_tilt_position"),
    "media_player": ("media_title", "media_artist", "volume_level", "source", "is_volume_muted"),
    "fan": ("percentage", "preset_mode", "oscillating"),
    "water_heater": ("temperature", "current_temperature", "operation_mode"),
    "humidifier": ("humidity", "current_humidity", "mode"),
    "weather": ("temperature", "humidity", "wind_speed", "pressure"),
    "vacuum": ("battery_level", "status", "fan_speed"),
    "lock": ("changed_by",),
    "person": ("source",),
    "camera": ("motion_detection",),
}

SAFETY_CLASSES = {"smoke", "gas", "carbon_monoxide", "moisture", "safety", "heat"}
OPENING_CLASSES = {"door", "window", "opening", "garage_door"}
DETECTION_CLASSES = {"motion", "occupancy", "presence"}
DETECTION_WORDS = ("person", "persoon", "people", "vehicle", "voertuig", "car", "visitor", "bezoeker",
                   "pet", "dier", "animal", "dog", "cat", "package", "pakket", "face")
# Only the "someone pressed the button" sensor. "deurbel"/"doorbell" is usually the device name, which the
# person/vehicle/motion sensors of the same doorbell also carry.
DOORBELL_WORDS = ("visitor", "bezoeker")

# Reolink (and others) expose each camera as several streams; the snapshot of the main stream is sharper.
# Cameras that look at the private back side of the house: someone there while the alarm is on is critical.
BACKYARD_WORDS = {"achtertuin", "achter", "tuin", "achterdeur", "schuur", "garden", "backyard", "back", "patio", "terras"}

HIGH_STREAM_WORDS = {"clear", "duidelijk", "main", "high", "hoog"}
LOW_STREAM_WORDS = {"fluent", "vloeiend", "sub", "low", "laag", "balanced", "gebalanceerd"}

# Readable states: small models misread "on"/"off" for doors, motion, smoke, ...
BINARY_STATES = {
    "door": ("open", "dicht"), "window": ("open", "dicht"), "opening": ("open", "dicht"),
    "garage_door": ("open", "dicht"), "lock": ("niet op slot", "op slot"),
    "motion": ("beweging", "geen beweging"), "occupancy": ("bezet", "vrij"), "presence": ("aanwezig", "afwezig"),
    "smoke": ("ROOK GEDETECTEERD", "ok"), "gas": ("GAS GEDETECTEERD", "ok"), "carbon_monoxide": ("CO GEDETECTEERD", "ok"),
    "moisture": ("NAT/LEKKAGE", "droog"), "safety": ("ONVEILIG", "veilig"), "battery": ("batterij bijna leeg", "batterij ok"),
    "connectivity": ("verbonden", "niet verbonden"), "problem": ("probleem", "ok"), "plug": ("ingeplugd", "uitgeplugd"),
    "power": ("stroom aan", "geen stroom"), "running": ("draait", "draait niet"), "vibration": ("trilling", "stil"),
}
ALARM_STATES = {
    "disarmed": "uitgeschakeld", "armed_away": "ingeschakeld (afwezig)", "armed_home": "ingeschakeld (thuis)",
    "armed_night": "ingeschakeld (nacht)", "armed_vacation": "ingeschakeld (vakantie)",
    "armed_custom_bypass": "ingeschakeld (aangepast)", "arming": "wordt ingeschakeld", "disarming": "wordt uitgeschakeld",
    "pending": "in afwachting", "triggered": "ALARM AFGEGAAN",
}
LOCK_STATES = {"locked": "op slot", "unlocked": "niet op slot", "open": "open", "jammed": "geblokkeerd",
               "locking": "wordt op slot gedaan", "unlocking": "wordt ontgrendeld"}

SKIP_DOMAINS = {"update", "persistent_notification", "conversation", "stt", "wake_word", "event"}


def _object_tokens(ent: "EntityInfo") -> set[str]:
    return set(ent.entity_id.split(".", 1)[1].split("_"))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class EntityInfo:
    entity_id: str
    name: str
    domain: str
    state: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)
    area: str | None = None
    device: str | None = None
    device_id: str | None = None
    aliases: list[str] = field(default_factory=list)
    device_class: str | None = None
    unit: str | None = None
    last_changed: str | None = None

    @property
    def search_text(self) -> str:
        parts = [self.entity_id.replace("_", " ").replace(".", " "), self.name, self.area or "", self.device or ""]
        parts += self.aliases
        if self.device_class:
            parts.append(self.device_class)
        return normalize(" ".join(parts))

    @property
    def readable_state(self) -> str:
        """State in words where the raw value is ambiguous (binary sensors, alarms, locks)."""
        if self.state in ("unknown", "unavailable"):
            return self.state
        if self.domain == "binary_sensor" and self.device_class in BINARY_STATES:
            on, off = BINARY_STATES[self.device_class]
            return on if self.state == "on" else off if self.state == "off" else self.state
        if self.domain == "alarm_control_panel":
            return ALARM_STATES.get(self.state, self.state)
        if self.domain == "lock":
            return LOCK_STATES.get(self.state, self.state)
        if self.domain in ("person", "device_tracker"):
            # Only "home" is home: any other zone (e.g. "Werk Vincent") means away.
            if self.state == "home":
                return "thuis"
            if self.state == "not_home":
                return "weg"
            return f"weg (in zone {self.state})"
        if self.unit:
            return f"{self.state} {self.unit}"
        return self.state

    def compact(self, with_attributes: bool = False) -> str:
        value = self.readable_state
        line = f"{self.entity_id} ({self.name}"
        if self.area:
            line += f", {self.area}"
        line += f") = {value}"
        if with_attributes:
            attrs = self.useful_attributes()
            if attrs:
                line += " | " + ", ".join(f"{k}={v}" for k, v in attrs.items())
        return line

    def useful_attributes(self) -> dict[str, Any]:
        keys = USEFUL_ATTRIBUTES.get(self.domain, ())
        attrs = {k: self.attributes[k] for k in keys if self.attributes.get(k) is not None}
        if self.domain == "light" and "brightness" in attrs:
            attrs["brightness_pct"] = round(attrs.pop("brightness") / 255 * 100)
        if self.device_class:
            attrs["device_class"] = self.device_class
        return attrs


class EntityIndex:
    def __init__(self) -> None:
        self.entities: dict[str, EntityInfo] = {}
        self.areas: dict[str, str] = {}  # area_id -> name
        self.services: dict[str, dict[str, Any]] = {}
        self.allowed: set[str] | None = None  # None = all entities allowed
        self.location_name: str | None = None
        self._registry: dict[str, dict[str, Any]] = {}
        self._device_area: dict[str, str | None] = {}
        self._device_name: dict[str, str] = {}

    # --- Building ---------------------------------------------------------

    def load(
        self,
        states: list[dict[str, Any]],
        areas: list[dict[str, Any]] | None = None,
        devices: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        services: dict[str, dict[str, Any]] | None = None,
        allowed: set[str] | None = None,
    ) -> None:
        self.areas = {a["area_id"]: a.get("name", a["area_id"]) for a in areas or []}
        self._device_area = {d["id"]: d.get("area_id") for d in devices or []}
        self._device_name = {
            d["id"]: d.get("name_by_user") or d.get("name") or "" for d in devices or []
        }
        self._registry = {e["entity_id"]: e for e in entities or []}
        self.services = services or {}
        self.allowed = allowed
        self.entities = {}
        for state in states:
            self.update_state(state)
        _LOGGER.info("Indexed %d entities in %d areas", len(self.entities), len(self.areas))

    def update_state(self, state: dict[str, Any] | None, entity_id: str | None = None) -> None:
        if state is None:
            if entity_id:
                self.entities.pop(entity_id, None)
            return
        eid = state["entity_id"]
        domain = eid.split(".", 1)[0]
        if domain in SKIP_DOMAINS:
            return
        if self.allowed is not None and eid not in self.allowed:
            return
        reg = self._registry.get(eid, {})
        if reg.get("hidden_by") or reg.get("entity_category") in ("config", "diagnostic"):
            # Diagnostic/config entities are hardly ever what a user means.
            if domain not in ("sensor", "binary_sensor"):
                return
        attrs = state.get("attributes", {})
        device_id = reg.get("device_id")
        area_id = reg.get("area_id") or (self._device_area.get(device_id) if device_id else None)
        self.entities[eid] = EntityInfo(
            entity_id=eid,
            name=attrs.get("friendly_name") or reg.get("name") or eid,
            domain=domain,
            state=state.get("state", "unknown"),
            attributes=attrs,
            area=self.areas.get(area_id) if area_id else None,
            device=self._device_name.get(device_id) if device_id else None,
            device_id=device_id,
            # Newer Home Assistant versions put None in the list for "the entity's own name".
            aliases=[str(a) for a in reg.get("aliases") or [] if a],
            device_class=attrs.get("device_class"),
            unit=attrs.get("unit_of_measurement"),
            last_changed=state.get("last_changed"),
        )

    def handle_state_changed(self, data: dict[str, Any]) -> None:
        self.update_state(data.get("new_state"), data.get("entity_id"))

    # --- Querying ---------------------------------------------------------

    def get(self, entity_id: str) -> EntityInfo | None:
        return self.entities.get(entity_id)

    def resolve_area(self, area: str) -> str | None:
        """Return the area name that best matches the given text."""
        target = normalize(area)
        best, best_score = None, 0.0
        for name in self.areas.values():
            n = normalize(name)
            score = 1.0 if n == target else SequenceMatcher(None, n, target).ratio()
            if target in n or n in target:
                score = max(score, 0.9)
            if score > best_score:
                best, best_score = name, score
        return best if best_score >= 0.7 else None

    def search(
        self,
        query: str = "",
        area: str | None = None,
        domain: str | None = None,
        device_class: str | None = None,
        limit: int = 15,
    ) -> list[EntityInfo]:
        candidates = list(self.entities.values())
        if domain:
            domains = {d.strip() for d in domain.split(",")}
            candidates = [e for e in candidates if e.domain in domains]
        if device_class:
            candidates = [e for e in candidates if e.device_class == device_class]
        if area:
            area_name = self.resolve_area(area)
            if area_name is None:
                return []
            candidates = [e for e in candidates if e.area == area_name]

        words = normalize(query).split()
        if not words:
            return sorted(candidates, key=lambda e: e.entity_id)[:limit]

        scored: list[tuple[float, EntityInfo]] = []
        for ent in candidates:
            text = ent.search_text
            tokens = text.split()
            score = 0.0
            for word in words:
                if word in tokens:
                    score += 2
                elif word in text:
                    score += 1.5
                else:
                    best = max((SequenceMatcher(None, word, t).ratio() for t in tokens), default=0)
                    if best >= 0.75:
                        score += best
            if score > 0:
                # Prefer controllable entities and shorter (more specific) names.
                if ent.domain not in ("sensor", "binary_sensor"):
                    score += 0.2
                score -= len(tokens) * 0.01
                scored.append((score, ent))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [ent for _, ent in scored[:limit]]

    def anyone_home(self) -> bool | None:
        """True/False based on person entities, None when there are none."""
        people = [e for e in self.entities.values() if e.domain == "person"]
        if not people:
            return None
        return any(p.state == "home" for p in people)

    def alarm_armed(self) -> bool:
        return any(e.domain == "alarm_control_panel" and e.state.startswith("armed")
                   for e in self.entities.values())

    @staticmethod
    def is_backyard(cam: EntityInfo) -> bool:
        words = set(normalize(f"{cam.name} {cam.area or ''}").split()) | _object_tokens(cam)
        return bool(words & BACKYARD_WORDS)

    def camera_profile(self, cam: EntityInfo, profiles: list[str]) -> str:
        """door | private | street | off, from the settings (any stream of the camera) or guessed from its name."""
        key = self._camera_key(cam)
        for item in profiles:
            entity_id, _, profile = item.partition("=")
            other = self.get(entity_id)
            if profile != "auto" and (entity_id == cam.entity_id or (other is not None and self._camera_key(other) == key)):
                return profile
        return "private" if self.is_backyard(cam) else "door"

    def is_detection_sensor(self, ent: EntityInfo) -> bool:
        if ent.domain != "binary_sensor":
            return False
        if ent.device_class in DETECTION_CLASSES:
            return True
        return bool(_object_tokens(ent) & set(DETECTION_WORDS))

    @staticmethod
    def is_doorbell(ent: EntityInfo) -> bool:
        return bool(_object_tokens(ent) & set(DOORBELL_WORDS))

    @staticmethod
    def stream_rank(cam: EntityInfo) -> int:
        tokens = _object_tokens(cam)
        if tokens & HIGH_STREAM_WORDS:
            return 0
        return 2 if tokens & LOW_STREAM_WORDS else 1

    @staticmethod
    def _camera_key(cam: EntityInfo) -> str:
        words = [w for w in normalize(cam.name).split() if w not in HIGH_STREAM_WORDS | LOW_STREAM_WORDS | {"camera"}]
        return " ".join(words) or cam.entity_id

    @staticmethod
    def camera_label(cam: EntityInfo) -> str:
        """Name without the stream: 'Deurbel Voordeur Vloeiend' -> 'Deurbel Voordeur'."""
        words = [w for w in cam.name.split() if normalize(w) not in HIGH_STREAM_WORDS | LOW_STREAM_WORDS]
        return " ".join(words) or cam.name

    def cameras(self, area: str | None = None, query: str | None = None, limit: int = 20) -> list[EntityInfo]:
        """Available cameras, one per physical camera (best stream), optionally filtered by area or text."""
        if query:
            found = self.search(query, area=area, domain="camera", limit=100)
        else:
            found = self.search(area=area, domain="camera", limit=500)
        best: dict[str, EntityInfo] = {}
        for cam in found:
            if cam.state == "unavailable":
                continue
            key = self._camera_key(cam)
            if key not in best or self.stream_rank(cam) < self.stream_rank(best[key]):
                best[key] = cam
        return list(best.values())[:limit]

    def best_stream(self, cam: EntityInfo) -> EntityInfo:
        """The sharpest available stream of the same physical camera."""
        key = self._camera_key(cam)
        siblings = [c for c in self.entities.values() if c.domain == "camera" and c.state != "unavailable"
                    and ((cam.device_id and c.device_id == cam.device_id) or self._camera_key(c) == key)]
        return min(siblings or [cam], key=lambda c: (self.stream_rank(c), c.entity_id))

    def camera_for(self, ent: EntityInfo) -> EntityInfo | None:
        """Camera on the same device (Reolink) or, failing that, in the same area."""
        cameras = [e for e in self.entities.values() if e.domain == "camera" and e.state != "unavailable"]
        if ent.device_id:
            same_device = [c for c in cameras if c.device_id == ent.device_id]
            if same_device:
                return min(same_device, key=lambda c: (self.stream_rank(c), c.entity_id))
        if ent.area:
            same_area = [c for c in cameras if c.area == ent.area]
            if same_area:
                return min(same_area, key=lambda c: (self.stream_rank(c), c.entity_id))
        return None

    def overview(self) -> dict[str, Any]:
        """Short summary of the house for the system prompt / overview tool."""
        by_domain: dict[str, int] = {}
        for ent in self.entities.values():
            by_domain[ent.domain] = by_domain.get(ent.domain, 0) + 1

        def ids(pred) -> list[str]:
            return [e.compact() for e in self.entities.values() if pred(e)]

        return {
            "location": self.location_name,
            "areas": sorted(self.areas.values()),
            "entity_counts": dict(sorted(by_domain.items())),
            "people": ids(lambda e: e.domain == "person"),
            "lights_on": ids(lambda e: e.domain == "light" and e.state == "on"),
            "open_doors_windows": ids(
                lambda e: e.domain == "binary_sensor"
                and e.device_class in OPENING_CLASSES
                and e.state == "on"
            ),
            "safety_alerts": ids(
                lambda e: e.domain == "binary_sensor" and e.device_class in SAFETY_CLASSES and e.state == "on"
            ),
            "weather": [e.compact(with_attributes=True) for e in self.entities.values() if e.domain == "weather"][:1],
            "climate": [e.compact(with_attributes=True) for e in self.entities.values() if e.domain == "climate"],
            "unlocked": ids(lambda e: e.domain == "lock" and e.state == "unlocked"),
            "alarms": ids(lambda e: e.domain == "alarm_control_panel"),
            "cameras": [c.compact() for c in self.cameras()],
            "unavailable_count": sum(1 for e in self.entities.values() if e.state == "unavailable"),
        }
