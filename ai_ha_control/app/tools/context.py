"""Context from outside the house itself: the calendar and energy prices / solar forecast (read-only)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..ha.index import EntityInfo, normalize
from .base import Tool, ToolContext

_LOGGER = logging.getLogger(__name__)

MAX_CALENDARS = 6
MAX_EVENTS = 12
# Keys used by price integrations (Nordpool, EnergyZero, Tibber, Frank Energie, Zonneplan, ...).
PRICE_LIST_ATTRIBUTES = ("raw_today", "raw_tomorrow", "prices_today", "prices_tomorrow", "prices", "today",
                         "tomorrow", "forecast")
TIME_KEYS = ("start", "from", "time", "datetime", "starts_at", "startsAt", "readingDate")
VALUE_KEYS = ("value", "price", "total", "electricity_price", "marketPrice", "price_ct_per_kwh")
FORECAST_WORDS = ("forecast", "verwacht", "estimated", "prognose", "solcast", "voorspel", "geschat")
# Most useful first: what is left today, then today and tomorrow; hourly values last.
FORECAST_ORDER = (("remaining", "resterend"), ("today", "vandaag"), ("tomorrow", "morgen"))


def _forecast_rank(ent: EntityInfo) -> int:
    text = normalize(f"{ent.entity_id} {ent.name}")
    return next((i for i, keys in enumerate(FORECAST_ORDER) if any(k in text for k in keys)), len(FORECAST_ORDER))


def _tz(ctx: ToolContext):
    return ZoneInfo(ctx.timezone) if ctx.timezone else timezone.utc


def _when(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    return str(value or "")


# --- Calendar -------------------------------------------------------------------------------

def calendars(ctx: ToolContext) -> list[EntityInfo]:
    return sorted((e for e in ctx.index.entities.values() if e.domain == "calendar"),
                  key=lambda e: e.entity_id)[:MAX_CALENDARS]


async def agenda(ctx: ToolContext, days: int = 1, start: datetime | None = None) -> list[str]:
    """Events from all calendars, 'dd-mm HH:MM Title (Calendar)'; all-day events without a time."""
    tz = _tz(ctx)
    start = start or datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        days = int(float(days))
    except (TypeError, ValueError):
        days = 1
    end = start + timedelta(days=max(1, min(days, 14)))
    rows: list[tuple[str, str]] = []
    for cal in calendars(ctx):
        try:
            events = await ctx.ha.calendar_events(cal.entity_id, start.isoformat(), end.isoformat())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Cannot read calendar %s: %s", cal.entity_id, err)
            continue
        for ev in events or []:
            begin = _when(ev.get("start"))
            try:
                moment = datetime.fromisoformat(begin)
                label = moment.astimezone(tz).strftime("%d-%m %H:%M") if "T" in begin else moment.strftime("%d-%m")
            except ValueError:
                label = begin
            rows.append((begin, f"{label} {ev.get('summary') or '?'} ({cal.name})"))
    return [text for _, text in sorted(rows)][:MAX_EVENTS]


async def get_agenda(ctx: ToolContext, days: int = 1) -> dict[str, Any]:
    if not calendars(ctx):
        return {"error": "geen agenda (calendar-entiteit) gevonden"}
    return {"events": await agenda(ctx, days) or ["geen afspraken"]}


# --- Energy -----------------------------------------------------------------------------------

def _price_points(value: Any) -> list[tuple[datetime, float]]:
    points = []
    if not isinstance(value, list):
        return points
    for item in value:
        if not isinstance(item, dict):
            continue
        moment = next((item[k] for k in TIME_KEYS if item.get(k)), None)
        price = next((item[k] for k in VALUE_KEYS if isinstance(item.get(k), (int, float))), None)
        if moment is None or price is None:
            continue
        try:
            when = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        points.append((when, float(price)))
    return points


def price_sensor(ctx: ToolContext) -> EntityInfo | None:
    from ..house import is_price_unit

    candidates = [e for e in ctx.index.entities.values() if e.domain == "sensor" and is_price_unit(e.unit)]
    with_lists = [e for e in candidates if any(_price_points(e.attributes.get(k)) for k in PRICE_LIST_ATTRIBUTES)]
    return (with_lists or candidates or [None])[0]


def cheapest_window(points: list[tuple[datetime, float]], hours: float, after: datetime) -> dict[str, Any] | None:
    """Cheapest run of consecutive slots lasting `hours`, starting at or after `after`."""
    points = sorted(p for p in points if p[0] >= after - timedelta(minutes=59))
    if len(points) < 2:
        return None
    slot = (points[1][0] - points[0][0]).total_seconds() or 3600
    size = max(1, round(hours * 3600 / slot))
    best = None
    for i in range(0, len(points) - size + 1):
        window = points[i:i + size]
        if (window[-1][0] - window[0][0]).total_seconds() > slot * (size - 1) + 1:
            continue  # gap in the data
        avg = sum(p for _, p in window) / size
        if window[0][0] + timedelta(seconds=slot) <= after:
            continue  # already started
        if best is None or avg < best[1]:
            best = (window[0][0], avg)
    if best is None:
        return None
    return {"start": best[0], "end": best[0] + timedelta(hours=hours), "avg_price": round(best[1], 4)}


def energy_info(ctx: ToolContext, hours: float = 3, now: datetime | None = None) -> dict[str, Any]:
    tz = _tz(ctx)
    now = now or datetime.now(tz)
    info: dict[str, Any] = {}
    sensor = price_sensor(ctx)
    if sensor is not None:
        points = [p for k in PRICE_LIST_ATTRIBUTES for p in _price_points(sensor.attributes.get(k))]
        info["price_now"] = f"{sensor.state} {sensor.unit} ({sensor.name})"
        window = cheapest_window(list(dict.fromkeys(points)), hours, now)
        if window:
            info[f"cheapest_{hours:g}h"] = (f"{window['start'].astimezone(tz):%d-%m %H:%M}-"
                                            f"{window['end'].astimezone(tz):%H:%M}, gem. {window['avg_price']} "
                                            f"{sensor.unit}")
        upcoming = [p for t, p in points if t >= now - timedelta(hours=1)]
        if upcoming:
            info["range_ahead"] = f"{min(upcoming):.3f} - {max(upcoming):.3f} {sensor.unit}"
    solar = [e for e in ctx.index.entities.values() if e.domain == "sensor" and e.device_class == "energy"
             and any(w in normalize(f"{e.name} {e.entity_id} {e.device or ''}") for w in FORECAST_WORDS)]
    if solar:
        solar.sort(key=lambda e: (_forecast_rank(e), e.entity_id))
        info["solar_forecast"] = [f"{e.name}: {e.state} {e.unit or ''}".strip() for e in solar[:4]]
    from ..house import grid_power, solar_power

    grid, pv = grid_power(ctx.index), solar_power(ctx.index)
    if grid is not None:
        watts = float(grid.state)
        info["grid_now"] = (f"{-watts:.0f} W teruglevering" if watts < 0 else f"{watts:.0f} W afname") + f" ({grid.name})"
    if pv is not None:
        info["solar_now"] = f"{float(pv.state):.0f} W ({pv.name})"
    return info


async def get_energy(ctx: ToolContext, hours: float = 3) -> dict[str, Any]:
    try:
        hours = max(0.25, min(float(hours), 12))
    except (TypeError, ValueError):
        hours = 3
    info = energy_info(ctx, hours)
    return info or {"error": "geen energieprijs- of zonneverwachting-sensor gevonden"}


CONTEXT_TOOLS = [
    Tool(
        "get_agenda",
        "Afspraken uit de agenda's in Home Assistant (vandaag of de komende dagen), bijv. vakantie, bezoek, "
        "vuilnisophaal.",
        {"type": "object", "properties": {
            "days": {"type": "integer", "description": "Aantal dagen vanaf vandaag (standaard 1)"}}},
        get_agenda,
    ),
    Tool(
        "get_energy",
        "Huidige stroomprijs, het goedkoopste tijdvak van de komende uren en de verwachte zonne-opbrengst. "
        "Gebruik dit voor vragen als 'wanneer kan de wasmachine het goedkoopst aan'. Alleen informatie, "
        "plan zelf niets in.",
        {"type": "object", "properties": {
            "hours": {"type": "number", "description": "Hoe lang het apparaat draait in uren (standaard 3)"}}},
        get_energy,
    ),
]
