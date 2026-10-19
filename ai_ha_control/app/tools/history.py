"""Exact answers to 'how often / when / how long' questions from Home Assistant's history.

Small models count badly: handed a list of state changes they guess, and they mistake the state at the
start of the period for something that happened. These tools do the counting themselves and return the
numbers and times ready to repeat.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..ha.index import EntityIndex, EntityInfo, _object_tokens, normalize
from .base import ToolContext

NO_DATA = {"unavailable", "unknown", ""}
MAX_DAYS = 31
MAX_TIMES = 25
MAX_PLACES = 8
DETECTION_MERGE_SECONDS = 60  # a person standing at the door makes the sensor flicker: that is one visit
# The state that counts as "active" when the question does not name one ("hoe lang stond X aan").
ACTIVE_STATES = ("on", "home", "playing", "cleaning", "open", "unlocked")
ON_OFF_DOMAINS = {"binary_sensor", "light", "switch", "fan", "input_boolean", "siren", "humidifier"}
STATE_ALIASES = {"aan": "on", "uit": "off", "thuis": "home", "weg": "not_home", "afwezig": "not_home",
                 "speelt": "playing", "dicht": "closed", "open": "open", "op slot": "locked"}

PERIODS = {
    "today": "today", "vandaag": "today", "yesterday": "yesterday", "gisteren": "yesterday",
    "eergisteren": "day_before_yesterday", "day before yesterday": "day_before_yesterday",
    "this week": "this_week", "deze week": "this_week", "week": "last_7_days", "last 7 days": "last_7_days",
    "afgelopen week": "last_7_days", "laatste 7 dagen": "last_7_days", "afgelopen 7 dagen": "last_7_days",
    "vannacht": "night", "night": "night", "last night": "night", "afgelopen nacht": "night",
    "month": "last_30_days", "maand": "last_30_days", "afgelopen maand": "last_30_days",
}
WEEKDAYS = {name: i for i, names in enumerate([
    ("maandag", "monday"), ("dinsdag", "tuesday"), ("woensdag", "wednesday"), ("donderdag", "thursday"),
    ("vrijdag", "friday"), ("zaterdag", "saturday"), ("zondag", "sunday")]) for name in names}
PERIOD_NAMES = ["today", "yesterday", "day_before_yesterday", "this_week", "last_7_days", "night",
                "last_30_days", "<weekdag>", "<JJJJ-MM-DD>"]

KIND_WORDS = {
    "person": {"person", "persoon", "personen", "people", "mens", "mensen", "iemand", "face", "gezicht"},
    "vehicle": {"vehicle", "voertuig", "voertuigen", "car", "auto", "autos"},
    "animal": {"pet", "huisdier", "dier", "dieren", "animal", "dog", "hond", "cat", "kat"},
    "package": {"package", "pakket", "pakketje", "pakketjes"},
    "doorbell": {"doorbell", "deurbel", "visitor", "bezoeker", "aanbellen", "aangebeld", "bel", "ring"},
    "motion": {"motion", "beweging", "bewegingen", "occupancy", "presence", "aanwezigheid"},
}
KIND_LABELS = {"person": "persoon", "vehicle": "voertuig", "animal": "dier", "package": "pakket",
               "doorbell": "aanbellen", "motion": "beweging", None: "detectie"}
FILLER_WORDS = {"bij", "de", "het", "een", "in", "op", "aan", "voor", "camera", "cam", "the", "at", "near"}


def tzinfo(ctx: ToolContext):
    return ZoneInfo(ctx.timezone) if ctx.timezone else timezone.utc


def local_now(tz) -> datetime:
    return datetime.now(tz)


# --- Periods --------------------------------------------------------------------------------------

def _parse_when(text: str, now: datetime, end: bool = False) -> datetime:
    text = str(text).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = map(int, text.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    try:
        value = datetime.fromisoformat(text)
    except ValueError as err:
        raise ValueError(f"tijd '{text}' niet begrepen; gebruik JJJJ-MM-DD, JJJJ-MM-DDTHH:MM of HH:MM") from err
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        value += timedelta(days=1)  # "until 2026-10-18" includes that whole day
    return value.astimezone(now.tzinfo)


def resolve_period(ctx: ToolContext, period: str | None = None, hours: float | None = None,
                   start: str | None = None, end: str | None = None,
                   default: str = "today") -> tuple[datetime, datetime, str]:
    """(begin, end, label) in local time. Raises ValueError for a period it does not understand."""
    tz = tzinfo(ctx)
    now = local_now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if start:
        begin = _parse_when(start, now)
        finish = _parse_when(end, now, end=True) if end else now
        clock = re.compile(r"\s*\d{1,2}:\d{2}\s*")
        if clock.fullmatch(str(start)) and begin > now:
            begin -= timedelta(days=1)  # "since 23:00" asked at 08:00 means last night
        if end and clock.fullmatch(str(end)) and finish <= begin:
            finish += timedelta(days=1)  # "23:00 to 06:00" runs past midnight
    elif hours:
        begin, finish = now - timedelta(hours=max(0.25, min(float(hours), 24 * MAX_DAYS))), now
    else:
        key = normalize(period or default)
        name = PERIODS.get(key, key.replace(" ", "_"))
        if name == "today":
            begin, finish = midnight, now
        elif name == "yesterday":
            begin, finish = midnight - timedelta(days=1), midnight
        elif name == "day_before_yesterday":
            begin, finish = midnight - timedelta(days=2), midnight - timedelta(days=1)
        elif name == "this_week":
            begin, finish = midnight - timedelta(days=now.weekday()), now
        elif name == "last_7_days":
            begin, finish = now - timedelta(days=7), now
        elif name == "last_30_days":
            begin, finish = now - timedelta(days=30), now
        elif name == "night":
            begin, finish = midnight - timedelta(hours=2), midnight + timedelta(hours=7)
        elif key in WEEKDAYS:
            days_back = (now.weekday() - WEEKDAYS[key]) % 7
            begin = midnight - timedelta(days=days_back)
            finish = begin + timedelta(days=1)
        elif re.fullmatch(r"\d{4} \d{2} \d{2}", key):
            begin = _parse_when(key.replace(" ", "-"), now)
            finish = begin + timedelta(days=1)
        else:
            raise ValueError(f"periode '{period}' onbekend; kies uit {', '.join(PERIOD_NAMES)} of geef start/end")
    finish = min(finish, now)
    begin = max(begin, now - timedelta(days=MAX_DAYS))
    if begin >= finish:
        raise ValueError("de periode ligt in de toekomst of is leeg")
    if begin.time() == finish.time() == datetime.min.time() and finish - begin == timedelta(days=1):
        label = f"{begin:%d-%m} (hele dag)"
    elif not spans_days(begin, finish):
        label = f"{begin:%d-%m} {begin:%H:%M}-{finish:%H:%M}"
    else:
        label = f"{begin:%d-%m %H:%M} tot {finish:%d-%m %H:%M}"
    return begin, finish, label


# --- Timelines --------------------------------------------------------------------------------------

def _timestamp(item: dict[str, Any]) -> datetime | None:
    if "lu" in item:
        return datetime.fromtimestamp(float(item.get("lc", item["lu"])), tz=timezone.utc)
    ts = item.get("last_changed") or item.get("last_updated")
    return datetime.fromisoformat(ts) if ts else None


@dataclass
class Timeline:
    initial: str | None  # state when the period began; None if Home Assistant did not know it
    changes: list[tuple[datetime, str]]  # real changes inside the period, oldest first


def build_timeline(items: list[dict[str, Any]], begin: datetime, end: datetime) -> Timeline:
    """Home Assistant's history starts with the state at the start time: that is not a change."""
    parsed = sorted((ts, str(item.get("s", item.get("state")))) for item in items if (ts := _timestamp(item)))
    initial, last, changes = None, None, []
    for ts, state in parsed:
        if ts > end:
            break
        if ts <= begin + timedelta(seconds=1):
            initial = last = state
            continue
        if state != last:
            changes.append((ts, state))
            last = state
    return Timeline(initial, changes)


@dataclass
class Episode:
    begin: datetime
    end: datetime | None  # None: still going when the period ended
    before_start: bool = False  # already going when the period began (not counted)
    resumed: bool = False  # came back after unavailable/unknown: the same episode


def episodes(timeline: Timeline, active: set[str], begin: datetime) -> list[Episode]:
    result: list[Episode] = []
    current = Episode(begin, None, before_start=True) if timeline.initial in active else None
    interrupted = False
    for ts, state in timeline.changes:
        if state in NO_DATA:
            if current is not None:
                current.end = ts
                result.append(current)
                current, interrupted = None, True
            continue
        if state in active:
            if current is None:
                current = Episode(ts, None, resumed=interrupted)
        elif current is not None:
            current.end = ts
            result.append(current)
            current = None
        if state not in active:
            interrupted = False
    if current is not None:
        result.append(current)
    return result


def merge(items: list[Episode], gap_seconds: float) -> list[Episode]:
    """Overlapping episodes (or ones close together) become one: duplicate sensors, flickering sensors."""
    merged: list[Episode] = []
    for ep in sorted(items, key=lambda e: e.begin):
        last = merged[-1] if merged else None
        if last is not None and (ep.resumed or last.end is None
                                 or (ep.begin - last.end).total_seconds() <= gap_seconds):
            if last.end is not None and (ep.end is None or ep.end > last.end):
                last.end = ep.end
            continue
        merged.append(replace(ep))
    return merged


def spans_days(begin: datetime, end: datetime) -> bool:
    return begin.date() != (end - timedelta(seconds=1)).date()


def _clock(dt: datetime, tz, multi_day: bool) -> str:
    dt = dt.astimezone(tz)
    return f"{dt:%d-%m %H:%M}" if multi_day else f"{dt:%H:%M}"


def duration_text(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} u {minutes % 60} min" if minutes % 60 else f"{minutes // 60} u"


def describe(items: list[Episode], end: datetime, tz, multi_day: bool) -> list[str]:
    rows = []
    for ep in items:
        start = "al actief bij begin periode" if ep.before_start else _clock(ep.begin, tz, multi_day)
        if ep.end is None:
            rows.append(f"{start} (nog bezig, {duration_text((end - ep.begin).total_seconds())})")
        else:
            rows.append(f"{start} tot {_clock(ep.end, tz, multi_day)} ({duration_text((ep.end - ep.begin).total_seconds())})")
    if len(rows) > MAX_TIMES:
        rows = [f"… {len(rows) - MAX_TIMES} eerdere niet getoond"] + rows[-MAX_TIMES:]
    return rows


# --- get_history ----------------------------------------------------------------------------------

def _readable(ent: EntityInfo, state: str) -> str:
    return replace(ent, state=state).readable_state


def _active_states(ent: EntityInfo, wanted: str | None, seen: set[str]) -> set[str]:
    if not wanted:
        if ent.domain in ON_OFF_DOMAINS:
            return {"on"}
        if ent.domain in ("person", "device_tracker"):
            return {"home"}
        return {s for s in ACTIVE_STATES if s in seen}
    word = normalize(wanted)
    matches = {s for s in seen if normalize(s) == word or normalize(_readable(ent, s)) == word
               or normalize(_readable(ent, s)).startswith(word + " ")}
    if word in STATE_ALIASES and STATE_ALIASES[word] in seen:
        matches.add(STATE_ALIASES[word])
    if word in ("weg", "afwezig", "away") and ent.domain in ("person", "device_tracker"):
        matches |= {s for s in seen if s not in ("home",) and s not in NO_DATA}
    return matches or {STATE_ALIASES.get(word, wanted)}


async def get_history(ctx: ToolContext, entity_id: str, hours: float | None = None, today: bool = False,
                      period: str | None = None, start: str | None = None, end: str | None = None,
                      state: str | None = None) -> dict[str, Any]:
    ent = ctx.index.get(entity_id)
    if ent is None:
        return {"error": f"entiteit '{entity_id}' bestaat niet; gebruik search_entities"}
    if not (today or period or start or hours):
        hours = 24
    try:
        begin, finish, label = resolve_period(ctx, "today" if today else period, hours, start, end)
    except ValueError as err:
        return {"error": str(err)}
    raw = await ctx.ha.history([entity_id], begin.astimezone(timezone.utc).isoformat(),
                               finish.astimezone(timezone.utc).isoformat())
    items = raw.get(entity_id, []) if isinstance(raw, dict) else []
    tz = tzinfo(ctx)
    summary: dict[str, Any] = {"entity": ent.compact(), "period": label,
                               "period_hours": round((finish - begin).total_seconds() / 3600, 2)}
    if ent.domain == "camera":
        summary["hint"] = ("De status van een camera (idle/recording/streaming) zegt niets over wie of wat er "
                           "gezien is. Gebruik get_detections voor personen, voertuigen, aanbellen of beweging.")

    values = []
    for item in items:
        try:
            values.append(float(item.get("s", item.get("state"))))
        except (TypeError, ValueError):
            pass
    if values and len(values) >= len(items) / 2:
        summary["samples"] = len(items)
        if ent.attributes.get("state_class") in ("total", "total_increasing"):
            # Meter (kWh, m³): the increase is what matters; a drop means the meter was reset.
            # A small dip (DSMR, Shelly rounding) is noise, not a reset: only a drop of more than 10% is.
            increase, base = 0.0, values[0]
            for value in values[1:]:
                if value >= base:
                    increase, base = increase + value - base, value
                elif value < 0.9 * base:
                    increase, base = increase + value, value
            summary.update({"increase": round(increase, 3), "last": values[-1], "unit": ent.unit})
            return summary
        summary.update({"min": round(min(values), 2), "max": round(max(values), 2),
                        "avg": round(sum(values) / len(values), 2), "first": values[0], "last": values[-1],
                        "unit": ent.unit})
        return summary

    timeline = build_timeline(items, begin, finish)
    multi_day = spans_days(begin, finish)
    seen = {s for _, s in timeline.changes} | ({timeline.initial} if timeline.initial else set())
    seen.discard(None)

    durations: dict[str, float] = defaultdict(float)
    entered: dict[str, int] = defaultdict(int)
    points = ([(begin, timeline.initial)] if timeline.initial is not None else []) + timeline.changes
    for i, (ts, st) in enumerate(points):
        nxt = points[i + 1][0] if i + 1 < len(points) else finish
        durations[_readable(ent, st)] += (nxt - ts).total_seconds()
    for _, st in timeline.changes:
        entered[_readable(ent, st)] += 1

    summary["state_at_start"] = _readable(ent, timeline.initial) if timeline.initial else "onbekend"
    summary["state_at_end"] = _readable(ent, points[-1][1]) if points else "onbekend"
    summary["number_of_changes"] = len(timeline.changes)
    summary["changes"] = [f"{_clock(ts, tz, multi_day)} {_readable(ent, st)}" for ts, st in timeline.changes][-20:]
    summary["times_changed_to"] = dict(entered)
    summary["duration_per_state"] = {k: duration_text(v) for k, v in durations.items() if v > 0}

    active = _active_states(ent, state, seen)
    if active:
        eps = merge(episodes(timeline, active, begin), 0)
        names = sorted({_readable(ent, s) for s in active})
        total = sum(((e.end or finish) - e.begin).total_seconds() for e in eps)
        summary["counted_state"] = " / ".join(names)
        summary["count"] = sum(1 for e in eps if not e.before_start)
        summary["total_duration"] = duration_text(total)
        summary["periods"] = describe(eps, finish, tz, multi_day)
        if eps and eps[0].before_start:
            summary["note"] = "Was al zo bij het begin van de periode; dat telt niet mee in count."
    if not timeline.changes:
        summary["note"] = (f"Geen enkele wijziging in deze periode; de hele periode "
                           f"'{summary['state_at_start']}'.")
    return summary


# --- get_detections -------------------------------------------------------------------------------

def sensor_kind(index: EntityIndex, ent: EntityInfo) -> str:
    if index.is_doorbell(ent):
        return "doorbell"
    tokens = _object_tokens(ent) | set(normalize(ent.name).split())
    for kind in ("person", "vehicle", "animal", "package"):
        if tokens & (KIND_WORDS[kind] - {"iemand"}):
            return kind
    return "motion"


def wanted_kind(what: str | None) -> str | None:
    words = set(normalize(what or "").split())
    for kind, kind_words in KIND_WORDS.items():
        if words & kind_words:
            return kind
    return None  # anything


@dataclass
class Place:
    key: str
    label: str
    camera: EntityInfo | None
    sensors: list[EntityInfo]


def detection_places(index: EntityIndex) -> dict[str, Place]:
    """Detection sensors grouped per physical camera (all its streams) or device."""
    places: dict[str, Place] = {}
    for ent in index.entities.values():
        if not index.is_detection_sensor(ent):
            continue
        cam = index.camera_for(ent)
        if cam is not None:
            cam = index.best_stream(cam)
            key, label = f"camera:{index._camera_key(cam)}", index.camera_label(cam)
        elif ent.device_id:
            key, label = f"device:{ent.device_id}", ent.device or ent.area or ent.name
        else:
            key, label = ent.entity_id, ent.area or ent.name
        place = places.setdefault(key, Place(key, label, cam, []))
        place.sensors.append(ent)
    return places


def _place_text(index: EntityIndex, place: Place) -> str:
    parts = [place.label] + [s.search_text for s in place.sensors]
    if place.camera is not None:
        key = index._camera_key(place.camera)
        parts += [c.search_text for c in index.entities.values()
                  if c.domain == "camera" and index._camera_key(c) == key]
    return normalize(" ".join(parts))


def match_places(index: EntityIndex, places: dict[str, Place], where: str) -> list[Place]:
    area = index.resolve_area(where)
    words = [w for w in normalize(where).split() if w not in FILLER_WORDS] or normalize(where).split()
    texts = {key: _place_text(index, p) for key, p in places.items()}
    found = [p for p in places.values() if area and (
        any(s.area == area for s in p.sensors) or (p.camera is not None and p.camera.area == area))]
    found += [p for p in places.values() if p not in found and all(w in texts[p.key] for w in words)]
    if not found:
        found = [p for p in places.values() if any(len(w) >= 4 and w in texts[p.key] for w in words)]
    return found


async def get_detections(ctx: ToolContext, what: str = "", where: str = "", period: str | None = None,
                         hours: float | None = None, start: str | None = None,
                         end: str | None = None) -> dict[str, Any]:
    index = ctx.index
    try:
        begin, finish, label = resolve_period(ctx, period, hours, start, end)
    except ValueError as err:
        return {"error": str(err)}
    places = detection_places(index)
    if not places:
        return {"error": "Home Assistant heeft geen detectie-sensoren (persoon/voertuig/beweging/deurbel). "
                         "Gebruik get_history op een andere sensor."}
    if where:
        target = index.get(where)
        if target is not None and target.domain == "binary_sensor":
            chosen = [p for p in places.values() if target in p.sensors]
        else:
            chosen = match_places(index, places, where.removeprefix("camera.").replace("_", " "))
        if not chosen:
            return {"error": f"geen detectie-sensoren gevonden bij '{where}'",
                    "places": sorted(p.label for p in places.values())}
    else:
        chosen = list(places.values())

    kind = wanted_kind(what)
    selected = [(p, [s for s in p.sensors if kind is None or sensor_kind(index, s) == kind]) for p in chosen]
    selected = [(p, sensors) for p, sensors in selected if sensors]
    if not selected:
        available = sorted({sensor_kind(index, s) for p in chosen for s in p.sensors})
        place_text = where or "de camera's"
        return {"error": f"geen {KIND_LABELS[kind]}-detectie bij {place_text}",
                "available_kinds": [KIND_LABELS[k] for k in available]}

    ids = [s.entity_id for _, sensors in selected for s in sensors]
    raw = await ctx.ha.history(ids, begin.astimezone(timezone.utc).isoformat(),
                               finish.astimezone(timezone.utc).isoformat())
    raw = raw if isinstance(raw, dict) else {}
    tz = tzinfo(ctx)
    multi_day = spans_days(begin, finish)
    results, total = [], 0
    for place, sensors in selected[:MAX_PLACES]:
        found: list[Episode] = []
        per_sensor = {}
        for sensor in sensors:
            eps = episodes(build_timeline(raw.get(sensor.entity_id, []), begin, finish), {"on"}, begin)
            per_sensor[sensor.entity_id] = sum(1 for e in eps if not e.before_start and not e.resumed)
            found += eps
        merged = merge(found, DETECTION_MERGE_SECONDS)
        counted = [e for e in merged if not e.before_start]
        total += len(counted)
        row: dict[str, Any] = {"place": place.label, "count": len(counted)}
        if counted:
            row["first"] = _clock(counted[0].begin, tz, multi_day)
            row["last"] = _clock(counted[-1].begin, tz, multi_day)
        row["times"] = describe(merged, finish, tz, multi_day)[-(MAX_TIMES if len(selected) == 1 else 10):]
        row["sensors"] = per_sensor
        if merged and merged[0].before_start:
            row["note"] = "Was al actief bij het begin van de periode; dat telt niet mee."
        if place.camera is not None:
            notes = ctx.db.events_between(begin.timestamp(), finish.timestamp(), kind="camera",
                                          entity_ids=[c.entity_id for c in index.entities.values()
                                                      if c.domain == "camera"
                                                      and index._camera_key(c) == index._camera_key(place.camera)])
            if notes:
                row["ai_descriptions"] = [f"{_clock(datetime.fromtimestamp(n['ts'], tz), tz, multi_day)} "
                                          f"{n['message'][:140]}" for n in notes[-8:]]
        results.append(row)

    answer = {"what": KIND_LABELS[kind], "period": label, "total": total, "places": results,
              "note": ("total/count is het exacte aantal keer dat de sensor afging (binnen 1 minuut "
                       "samengevoegd tot één keer); neem het letterlijk over en tel niet zelf. Tijden zijn "
                       "lokale tijd. ai_descriptions zijn beschrijvingen van de AI bij een deel van de "
                       "detecties, niet geschikt om te tellen.")}
    if total == 0:
        answer["answer_hint"] = f"In deze periode is er geen {KIND_LABELS[kind]} gedetecteerd."
    if len(selected) > MAX_PLACES:
        answer["omitted_places"] = len(selected) - MAX_PLACES
    return answer
