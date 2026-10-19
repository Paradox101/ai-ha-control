"""Knowing what is normal: house mode, learned sensor baselines and anomalies. No LLM involved."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..ha.index import DETECTION_CLASSES, EntityInfo, normalize
from ..notify import in_quiet_hours

if TYPE_CHECKING:
    from ..config import Config
    from ..ha.index import EntityIndex
    from ..storage.db import Database

_LOGGER = logging.getLogger(__name__)

MODE_HOME, MODE_AWAY, MODE_SLEEP, MODE_VACATION, MODE_GUEST = "thuis", "weg", "slapen", "vakantie", "gast"
VACATION_HOURS = 24
# Scenes that represent a house mode (often switched by automations): the most recently activated one wins.
# A sleep scene counts for one night at most.
SLEEP_SCENE_HOURS = 12
MODE_SCENE_WORDS = {
    MODE_AWAY: ("afwezig", "weg", "away", "vertrek"),
    MODE_SLEEP: ("slapen", "nacht", "sleep", "night", "welterusten"),
    MODE_VACATION: ("vakantie", "vacation", "holiday"),
    MODE_GUEST: ("gast", "gasten", "guest", "logee"),
    MODE_HOME: ("thuis", "home", "aanwezig", "welkom"),
}

# Numeric sensors worth learning, with the smallest deviation that is ever interesting.
MIN_DEVIATION = {
    "temperature": 3.0,
    "humidity": 15.0,
    "power": 300.0,
    "carbon_dioxide": 400.0,
    "pm25": 25.0,
    "pm10": 40.0,
    "volatile_organic_compounds": 300.0,
    "moisture": 15.0,
    "water": 50.0,
}
MIN_SAMPLES = 30          # per hour-of-day bucket before we judge
Z_THRESHOLD = 4.0
SAMPLE_INTERVAL = 600     # seconds between samples of the same sensor
LEARNING_DAYS = 14        # activity at a never-seen hour only counts after two weeks of observation
OFFLINE_MINUTES = 60
OFFLINE_DOMAINS = {"sensor", "binary_sensor", "lock", "camera", "climate", "alarm_control_panel"}
HOUSE_OFFLINE_DOMAINS = {"lock", "camera", "climate", "alarm_control_panel"}  # always, also without an area


def day_bucket(now: datetime) -> int:
    """Baseline bucket per kind of day: 100-123 working days, 124-147 weekend (0-23 = all days)."""
    return (124 if now.weekday() >= 5 else 100) + now.hour


def minutes_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        changed = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if changed.tzinfo is None:
        return 0.0
    return (time.time() - changed.timestamp()) / 60


def scene_mode(index: EntityIndex) -> tuple[str, datetime] | None:
    """The mode of the most recently activated mode scene (a scene's state is its last activation time)."""
    latest: tuple[str, datetime] | None = None
    for ent in index.entities.values():
        if ent.domain != "scene":
            continue
        words = set(ent.name.lower().replace("_", " ").split()) | set(ent.entity_id.split(".", 1)[1].split("_"))
        mode = next((m for m, keys in MODE_SCENE_WORDS.items() if words & set(keys)), None)
        if mode is None:
            continue
        try:
            activated = datetime.fromisoformat(ent.state)
        except ValueError:
            continue  # never activated
        if latest is None or activated > latest[1]:
            latest = (mode, activated)
    return latest


def calendar_mode(index: EntityIndex) -> str | None:
    """Vacation or guests from a calendar event that is going on right now (its title)."""
    for ent in index.entities.values():
        if ent.domain != "calendar" or ent.state != "on":
            continue
        words = set(normalize(str(ent.attributes.get("message") or "")).split())
        for mode in (MODE_VACATION, MODE_GUEST):
            if words & set(MODE_SCENE_WORDS[mode]):
                return mode
    return None


def helper_mode(index: EntityIndex, config: Config) -> str | None:
    """The resident's own house mode helper (input_text.actieve_scene = 'afwezig', ...)."""
    from ..house import mode_helper

    ent = mode_helper(index, config)
    if ent is None or ent.state in ("unknown", "unavailable", ""):
        return None
    words = set(normalize(ent.state).split())
    mode = next((m for m, keys in MODE_SCENE_WORDS.items() if words & set(keys)), None)
    if mode == MODE_AWAY and minutes_since(ent.last_changed) / 60 >= VACATION_HOURS:
        return MODE_VACATION
    return mode


def house_mode(index: EntityIndex, config: Config, now: datetime) -> str:
    people = [e for e in index.entities.values() if e.domain == "person"]
    from_helper = helper_mode(index, config)
    if from_helper in (MODE_AWAY, MODE_VACATION) and any(p.state == "home" for p in people):
        from_helper = None  # came home before the helper was switched back: presence wins
    if from_helper:
        return from_helper
    from_scene = scene_mode(index)
    if from_scene:
        mode, activated = from_scene
        hours = (time.time() - activated.timestamp()) / 3600
        if mode in (MODE_AWAY, MODE_VACATION) and any(p.state == "home" for p in people):
            pass  # came home without switching the scene back: presence wins (no burglary alarm at home)
        elif mode == MODE_SLEEP and hours >= SLEEP_SCENE_HOURS:
            pass  # last night's sleep scene does not make the whole day a night
        elif mode == MODE_AWAY and hours >= VACATION_HOURS:
            return MODE_VACATION
        else:
            return mode
    from_calendar = calendar_mode(index) if config.calendar_context else None
    if people and all(p.state != "home" for p in people):
        away_hours = min(minutes_since(p.last_changed) for p in people) / 60
        if from_calendar == MODE_VACATION:
            return MODE_VACATION
        return MODE_VACATION if away_hours >= VACATION_HOURS else MODE_AWAY
    if in_quiet_hours(config.sleep_hours, now):
        return MODE_SLEEP
    if from_calendar == MODE_GUEST:
        return MODE_GUEST
    return MODE_HOME


@dataclass
class Anomaly:
    key: str          # stable id, used for muting ("dit is normaal")
    entity_id: str
    text: str
    severity: str = "warning"


class Baseline:
    def __init__(self, db: Database, index: EntityIndex, config: Config | None = None) -> None:
        self._db = db
        self._index = index
        self._config = config
        self._last_sample: dict[str, float] = {}
        self._reported_offline: set[str] = set()
        self._seen_available: set[str] = set()

    def learnable(self, ent: EntityInfo) -> bool:
        """Sensors of the house: a busy server or container is no anomaly worth an incident."""
        from ..house import is_hardware

        if ent.domain != "sensor" or ent.device_class not in MIN_DEVIATION or is_hardware(ent):
            return False
        ignored = {a.lower() for a in getattr(self._config, "offline_ignore_areas", None) or []}
        return not (ent.area and ent.area.lower() in ignored)

    def _emit(self, anomaly: Anomaly) -> Anomaly | None:
        return None if self._db.is_muted(anomaly.key) else anomaly

    def sample(self, now: datetime) -> list[Anomaly]:
        """Learn from numeric sensors and return values far outside what is normal for this hour."""
        anomalies = []
        mono = time.monotonic()
        for ent in list(self._index.entities.values()):
            if not self.learnable(ent):
                continue
            if mono - self._last_sample.get(ent.entity_id, -1e9) < SAMPLE_INTERVAL:
                continue
            try:
                value = float(ent.state)
            except ValueError:
                continue
            self._last_sample[ent.entity_id] = mono
            n, mean, variance = self._db.baseline_update(ent.entity_id, now.hour, value)
            # Weekends differ from working days: judge against the same kind of day once that is learned.
            day_n, day_mean, day_variance = self._db.baseline_update(ent.entity_id, day_bucket(now), value)
            if day_n >= MIN_SAMPLES:
                n, mean, variance = day_n, day_mean, day_variance
            if n < MIN_SAMPLES:
                continue
            deviation = value - mean
            limit = max(Z_THRESHOLD * math.sqrt(variance), MIN_DEVIATION[ent.device_class])
            if abs(deviation) > limit:
                direction = "hoog" if deviation > 0 else "laag"
                unit = f" {ent.unit}" if ent.unit else ""
                anomaly = self._emit(Anomaly(
                    key=f"anomaly:{ent.entity_id}:{direction}",
                    entity_id=ent.entity_id,
                    text=f"{ent.name} is ongewoon {direction}: {value:g}{unit} "
                         f"(normaal rond {mean:.1f}{unit} om {now.hour}:00"
                         f"{' in het weekend' if day_n >= MIN_SAMPLES and now.weekday() >= 5 else ''})",
                ))
                if anomaly:
                    anomalies.append(anomaly)
        return anomalies

    def activity(self, ent: EntityInfo, now: datetime) -> Anomaly | None:
        """A motion/door sensor active at an hour where it has never been active before."""
        from ..house import is_geofence

        if ent.domain != "binary_sensor" or is_geofence(ent):
            return None
        if ent.device_class not in DETECTION_CLASSES and not self._index.is_opening(ent):
            return None
        first_seen = self._db.first_seen(ent.entity_id)
        before = self._db.activity_increment(ent.entity_id, now.hour)
        if before > 0 or time.time() - first_seen < LEARNING_DAYS * 86400:
            return None
        return self._emit(Anomaly(
            key=f"anomaly:{ent.entity_id}:hour{now.hour}",
            entity_id=ent.entity_id,
            text=f"{ent.name} is actief om {now:%H:%M}; dat is op dit uur nog nooit gebeurd",
        ))

    def _watch_offline(self, ent: EntityInfo) -> bool:
        """Only devices of the house: not the server room, not container/host statistics without a room."""
        config = self._config
        if config is None:
            return True
        if ent.area and ent.area.lower() in {a.lower() for a in config.offline_ignore_areas}:
            return False
        return not config.offline_house_only or bool(ent.area) or ent.domain in HOUSE_OFFLINE_DOMAINS

    def offline(self) -> list[Anomaly]:
        """Devices that went unavailable while we were watching and stayed so (once per outage).

        Entities that were already unavailable when we started are ignored: big installations
        always have some permanently dead entities.
        """
        dropped = []
        for ent in list(self._index.entities.values()):
            if ent.domain not in OFFLINE_DOMAINS or not self._watch_offline(ent):
                continue
            if ent.state != "unavailable":
                self._seen_available.add(ent.entity_id)
                self._reported_offline.discard(ent.entity_id)
                continue
            if ent.entity_id not in self._seen_available or ent.entity_id in self._reported_offline:
                continue
            if minutes_since(ent.last_changed) < OFFLINE_MINUTES:
                continue
            self._reported_offline.add(ent.entity_id)
            if not self._db.is_muted(f"anomaly:{ent.entity_id}:offline"):
                dropped.append(ent)
        if not dropped:
            return []
        if len(dropped) > 3:
            names = ", ".join(e.name for e in dropped[:5])
            return [Anomaly(key="anomaly:many:offline", entity_id=dropped[0].entity_id, severity="info",
                            text=f"{len(dropped)} apparaten zijn al meer dan een uur onbereikbaar ({names}, …)")]
        return [Anomaly(key=f"anomaly:{e.entity_id}:offline", entity_id=e.entity_id, severity="info",
                        text=f"{e.name} is al {int(minutes_since(e.last_changed))} minuten onbereikbaar")
                for e in dropped]
