"""A realistic day in the test house, for 'how often / when / how long' questions.

Monday 19-10-2026, 15:30 in Amsterdam. Used by the tests and by scripts/benchmark.py (real models).
The front door camera has sensors on its device and duplicate per-stream (Duidelijk/Vloeiend) person
sensors; one person sensor briefly goes unavailable during a visit. Expected answers are in EVAL_CASES.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 10, 19, 15, 30, tzinfo=TZ)
YESTERDAY = NOW - timedelta(days=1)


def at(clock: str, day: datetime = NOW) -> datetime:
    hour, minute, *second = map(int, clock.split(":"))
    return day.replace(hour=hour, minute=minute, second=second[0] if second else 0)


def on_off(*pairs: tuple[str, str], day: datetime = NOW) -> list[tuple[datetime, str]]:
    rows = []
    for start, end in pairs:
        rows += [(at(start, day), "on"), (at(end, day), "off")]
    return rows


EXTRA_ENTITIES = [
    # (entity_id, friendly name, device_id, area_id, device_class)
    ("binary_sensor.voordeur_duidelijk_persoon", "Voordeur Duidelijk Persoon", "cam1", None, None),
    ("binary_sensor.voordeur_vloeiend_persoon", "Voordeur Vloeiend Persoon", "cam1", None, None),
    ("binary_sensor.voordeur_beweging", "Voordeur Beweging", "cam1", None, "motion"),
    ("camera.achtertuin", "Achtertuin", "cam2", "achtertuin", None),
    ("binary_sensor.achtertuin_beweging", "Achtertuin Beweging", "cam2", "achtertuin", "motion"),
    ("binary_sensor.achtertuin_persoon", "Achtertuin Persoon", "cam2", "achtertuin", None),
]

TIMELINES: dict[str, list[tuple[datetime, str]]] = {
    "binary_sensor.voordeur_person": [
        (at("00:00", YESTERDAY), "off"),
        *on_off(("19:20", "19:21"), day=YESTERDAY),
        *on_off(("08:12:05", "08:12:40"), ("08:12:55", "08:13:30"), ("12:47:10", "12:48:00")),
        (at("14:05:00"), "on"), (at("14:05:20"), "unavailable"), (at("14:05:30"), "on"), (at("14:06:10"), "off"),
    ],
    "binary_sensor.voordeur_duidelijk_persoon": [
        (at("00:00", YESTERDAY), "off"),
        *on_off(("08:12:06", "08:13:20"), ("12:47:11", "12:47:58"), ("14:05:01", "14:06:05")),
    ],
    "binary_sensor.voordeur_vloeiend_persoon": [
        (at("00:00", YESTERDAY), "off"), *on_off(("08:12:07", "08:13:25"), ("12:47:12", "12:47:59")),
    ],
    "binary_sensor.voordeur_visitor": [(at("00:00", YESTERDAY), "off"), *on_off(("12:47:30", "12:47:33"))],
    "binary_sensor.voordeur_beweging": [
        (at("00:00", YESTERDAY), "off"), *on_off(("08:12:00", "08:14:00"), ("12:47:00", "12:48:30"),
                                                ("14:04:50", "14:06:30")),
    ],
    "binary_sensor.achtertuin_beweging": [
        (at("00:00", YESTERDAY), "off"), *on_off(("03:14", "03:15"), ("10:00", "10:02"), ("10:30", "10:31")),
    ],
    "binary_sensor.achtertuin_persoon": [(at("00:00", YESTERDAY), "off"), *on_off(("03:14:10", "03:14:40"))],
    "binary_sensor.raam_slaapkamer": [
        (at("21:00", YESTERDAY), "off"), (at("22:00", YESTERDAY), "on"),
        (at("07:30"), "off"), (at("09:00"), "on"), (at("09:45"), "off"), (at("15:00"), "on"),
    ],
    "person.vincent": [(at("18:00", YESTERDAY), "home"), (at("08:30"), "not_home"), (at("14:50"), "home")],
    "light.keuken": [(at("23:00", YESTERDAY), "off"), (at("07:00"), "on"), (at("08:25"), "off"), (at("15:10"), "on")],
    "camera.voordeur_clear": [(at("00:00", YESTERDAY), "idle")],
}


def load_day(index: Any, ha: Any) -> None:
    """Add the extra entities to the test house and give the fake Home Assistant a day of history."""
    index.areas["achtertuin"] = "Achtertuin"
    for eid, name, device, area, device_class in EXTRA_ENTITIES:
        index._registry[eid] = {"entity_id": eid, "device_id": device, "area_id": area}
        attributes = {"friendly_name": name, **({"device_class": device_class} if device_class else {})}
        index.update_state({"entity_id": eid, "state": "off" if eid.startswith("binary") else "idle",
                            "attributes": attributes})
    ha.timelines.update(TIMELINES)


@dataclass
class EvalCase:
    question: str
    tool: str  # the call a good model makes
    arguments: dict[str, Any]
    expect: dict[str, Any]  # field -> value in the tool result
    contains: list[str] = field(default_factory=list)  # text that must appear in the tool result
    answer: list[tuple[str, ...]] = field(default_factory=list)  # benchmark: each group, one must be in the answer


EVAL_CASES = [
    EvalCase("Hoe vaak is er vandaag een persoon gezien bij de voordeur?", "get_detections",
             {"what": "person", "where": "voordeur"}, {"total": 3},
             ["08:12", "12:47", "14:05"], [("3", "drie")]),
    EvalCase("Hoe laat is er vandaag aangebeld?", "get_detections", {"what": "doorbell"}, {"total": 1},
             ["12:47"], [("12:47",)]),
    EvalCase("Was er gisteren iemand bij de voordeur?", "get_detections",
             {"what": "persoon", "where": "voordeur", "period": "yesterday"}, {"total": 1}, ["19:20"],
             [("19:20",)]),
    EvalCase("Is er vannacht iemand in de achtertuin geweest?", "get_detections",
             {"what": "person", "where": "achtertuin", "period": "vannacht"}, {"total": 1}, ["03:14"],
             [("03:14",)]),
    EvalCase("Hoe vaak was er vandaag beweging in de achtertuin?", "get_detections",
             {"what": "motion", "where": "achtertuin"}, {"total": 3}, ["03:14", "10:00", "10:30"],
             [("3", "drie")]),
    EvalCase("Hoeveel keer is er vandaag in totaal een persoon gezien?", "get_detections", {"what": "person"},
             {"total": 4}, [], [("4", "vier")]),
    EvalCase("Wanneer kwam Vincent vandaag thuis?", "get_history",
             {"entity_id": "person.vincent", "period": "today", "state": "home"}, {"count": 1}, ["14:50"],
             [("14:50",)]),
    EvalCase("Hoe lang is Vincent vandaag weg geweest?", "get_history",
             {"entity_id": "person.vincent", "period": "today", "state": "weg"},
             {"count": 1, "total_duration": "6 u 20 min"}, ["08:30"], [("6 u", "6 uur", "zes uur"), ("20",)]),
    EvalCase("Hoe lang heeft het raam in de slaapkamer vandaag opengestaan?", "get_history",
             {"entity_id": "binary_sensor.raam_slaapkamer", "period": "today"},
             {"count": 2, "total_duration": "8 u 45 min"}, ["09:00", "15:00"],
             [("8 u", "8 uur", "acht uur"), ("45",)]),
    EvalCase("Hoe vaak ging de keukenlamp vandaag aan?", "get_history",
             {"entity_id": "light.keuken", "today": True}, {"count": 2, "total_duration": "1 u 45 min"},
             ["07:00", "15:10"], [("2", "twee")]),
    EvalCase("Hoe vaak ging de deurbel gisteren?", "get_detections", {"what": "aanbellen", "period": "gisteren"},
             {"total": 0}, ["geen"], [("geen", "niet", "0", "nul")]),
]
