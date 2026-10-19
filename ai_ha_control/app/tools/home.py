"""Tools the LLM can use to read and control the house."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..ha.index import normalize
from ..house import car_status, outside_temperature, room_readings
from .actions import perform
from .agent_tools import AGENT_TOOLS, HOUSE_RULE_TOOLS
from .automations import TOOLS as AUTOMATION_TOOLS
from .base import Tool, ToolContext, ToolRegistry, as_list
from .context import CONTEXT_TOOLS
from .history import get_detections, get_history

# --- Reading -----------------------------------------------------------------


async def get_home_overview(ctx: ToolContext) -> dict[str, Any]:
    overview = ctx.index.overview()
    # Per room the right sensor (room median or the comfort profile's choice); rooms without one are left out.
    overview["room_readings"] = room_readings(ctx.index, ctx.db.profiles())
    outside = outside_temperature(ctx.index, ctx.config)
    if outside is not None:
        overview["outside_temperature"] = round(outside, 1)
    car = car_status(ctx.index, ctx.config.language == "nl")
    if car:
        overview["car"] = car
    return overview


async def search_entities(
    ctx: ToolContext,
    query: str = "",
    area: str | None = None,
    domain: str | None = None,
    device_class: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    results = ctx.index.search(query, area=area, domain=domain, device_class=device_class, limit=min(int(limit), 40))
    if not results and area and ctx.index.resolve_area(area) is None:
        return {"results": [], "hint": f"ruimte '{area}' niet gevonden", "areas": sorted(ctx.index.areas.values())}
    return {"results": [e.compact() for e in results]}


async def get_state(ctx: ToolContext, entity_ids: Any) -> dict[str, Any]:
    found, missing = [], []
    for eid in as_list(entity_ids):
        ent = ctx.index.get(eid)
        if ent:
            found.append(ent.compact(with_attributes=True))
        else:
            missing.append(eid)
    result: dict[str, Any] = {"states": found}
    if missing:
        result["not_found"] = missing
    return result


async def get_forecast(ctx: ToolContext, entity_id: str | None = None, kind: str = "daily") -> dict[str, Any]:
    if entity_id is None:
        weather = ctx.index.search(domain="weather", limit=1)
        if not weather:
            return {"error": "geen weer-entiteit gevonden"}
        entity_id = weather[0].entity_id
    kind = kind if kind in ("daily", "hourly", "twice_daily") else "daily"
    response = await ctx.ha.call_service(
        "weather", "get_forecasts", {"type": kind}, {"entity_id": [entity_id]}, return_response=True
    )
    forecast = ((response or {}).get(entity_id) or {}).get("forecast") or []
    keys = ("condition", "temperature", "templow", "precipitation", "precipitation_probability", "wind_speed")
    tz = ZoneInfo(ctx.timezone) if ctx.timezone else timezone.utc
    rows = []
    for item in forecast[: 7 if kind != "hourly" else 12]:
        when = datetime.fromisoformat(item["datetime"]).astimezone(tz)
        label = when.strftime("%a %d-%m") if kind != "hourly" else when.strftime("%H:%M")
        rows.append(label + ": " + ", ".join(f"{k}={item[k]}" for k in keys if item.get(k) is not None))
    current = ctx.index.get(entity_id)
    return {"now": current.compact(with_attributes=True) if current else None, "forecast": rows}


# --- Controlling -------------------------------------------------------------

COLOR_NAMES = {
    "rood": "red", "groen": "green", "blauw": "blue", "geel": "yellow", "oranje": "orange",
    "paars": "purple", "roze": "pink", "wit": "white", "warmwit": "warmwhite",
}


async def control_light(
    ctx: ToolContext,
    entity_ids: Any,
    action: str = "on",
    brightness_pct: int | None = None,
    color_name: str | None = None,
    color_temp_kelvin: int | None = None,
) -> dict[str, Any]:
    ids = as_list(entity_ids)
    if not ids:
        return {"error": "geen entity_ids opgegeven"}
    action = action.lower()
    if action in ("off", "uit"):
        return await perform(ctx, "light", "turn_off", ids)
    if action == "toggle":
        return await perform(ctx, "light", "toggle", ids)
    data: dict[str, Any] = {}
    if brightness_pct is not None:
        data["brightness_pct"] = max(0, min(100, int(brightness_pct)))
    if color_name:
        name = COLOR_NAMES.get(color_name.lower(), color_name.lower())
        if name == "warmwhite":
            data["color_temp_kelvin"] = 2700
        else:
            data["color_name"] = name
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = int(color_temp_kelvin)
    return await perform(ctx, "light", "turn_on", ids, data)


async def set_climate(
    ctx: ToolContext,
    entity_id: str,
    temperature: float | None = None,
    hvac_mode: str | None = None,
    preset_mode: str | None = None,
) -> dict[str, Any]:
    ent = ctx.index.get(entity_id)
    if ent is None or ent.domain != "climate":
        return {"error": f"'{entity_id}' is geen climate-entiteit; gebruik search_entities met domain=climate"}
    # Check everything first: a half-applied request reported as an error misleads the model and the user.
    modes = ent.attributes.get("hvac_modes") or []
    if hvac_mode and modes and hvac_mode not in modes:
        return {"error": f"hvac_mode '{hvac_mode}' niet ondersteund. Opties: {modes}"}
    presets = ent.attributes.get("preset_modes") or []
    if preset_mode and presets and preset_mode not in presets:
        return {"error": f"preset '{preset_mode}' niet ondersteund. Opties: {presets}"}
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError):
            return {"error": f"temperatuur '{temperature}' is geen getal"}
        lo, hi = ent.attributes.get("min_temp", 5), ent.attributes.get("max_temp", 30)
        if not lo <= temperature <= hi:
            return {"error": f"temperatuur {temperature} buiten bereik {lo}-{hi}"}
    results = {}
    if hvac_mode:
        results["hvac_mode"] = await perform(ctx, "climate", "set_hvac_mode", [entity_id], {"hvac_mode": hvac_mode})
    if preset_mode:
        results["preset_mode"] = await perform(
            ctx, "climate", "set_preset_mode", [entity_id], {"preset_mode": preset_mode}
        )
    if temperature is not None:
        results["temperature"] = await perform(
            ctx, "climate", "set_temperature", [entity_id], {"temperature": temperature}
        )
    if not results:
        return {"error": "geef temperature, hvac_mode of preset_mode op"}
    return results


async def turn_on_off(ctx: ToolContext, entity_ids: Any, action: str = "on") -> dict[str, Any]:
    ids = as_list(entity_ids)
    if not ids:
        return {"error": "geen entity_ids opgegeven"}
    action = {"aan": "on", "uit": "off"}.get(action.lower(), action.lower())
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
    if service is None:
        return {"error": "action moet on, off of toggle zijn"}
    by_domain: dict[str, list[str]] = defaultdict(list)
    for eid in ids:
        by_domain[eid.split(".", 1)[0]].append(eid)
    return {domain: await perform(ctx, domain, service, eids) for domain, eids in by_domain.items()}


async def call_service(
    ctx: ToolContext,
    domain: str,
    service: str,
    entity_ids: Any = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if "." in service and not domain:
        domain, service = service.split(".", 1)
    elif service.startswith(f"{domain}."):
        service = service.split(".", 1)[1]
    return await perform(ctx, domain, service, as_list(entity_ids), data if isinstance(data, dict) else None)


MAX_CAMERAS_PER_QUESTION = 3


async def analyze_camera(
    ctx: ToolContext, entity_id: str | None = None, area: str | None = None, question: str = ""
) -> dict[str, Any]:
    index = ctx.index
    if not entity_id and not area:
        return {"error": "geef een ruimte (area) of camera (entity_id) op",
                "cameras": [c.compact() for c in index.cameras()]}
    ent = index.get(entity_id) if entity_id else None
    if ent is not None and ent.domain == "camera":
        targets = [index.best_stream(ent)]
    else:
        # A guessed entity_id ("camera.achtertuin") or an area name: find the matching camera(s).
        text = (entity_id or "").removeprefix("camera.").replace("_", " ")
        targets = index.cameras(area=area, query=text or None) if area and index.resolve_area(area) else []
        if not targets:
            query = " ".join(filter(None, [area, text]))
            targets = index.cameras(query=query)
            # Prefer cameras matching every word ("achtertuin poort" → not the "achtertuin huis" camera).
            words = normalize(query).split()
            exact = [c for c in targets if all(w in c.search_text for w in words)]
            targets = exact or targets
    if not targets:
        return {"error": f"geen camera gevonden voor '{area or entity_id}'",
                "cameras": [c.compact() for c in index.cameras()]}
    # Bedroom, bathroom and baby cameras ("off"): only when a person asks for exactly that camera, never by the
    # agent on its own and never as a side effect of asking about a room.
    asked_exactly = ent is not None and ent.domain == "camera" and ctx.source in ("web", "assist")
    allowed = [c for c in targets if asked_exactly or index.camera_profile(c, ctx.config.camera_profiles) != "off"]
    if not allowed:
        return {"error": "deze camera kijkt in een slaapkamer of badkamer (profiel 'uit'); daar kijk ik niet "
                         "zelf mee. Vraag het met de exacte camera-entiteit als je het echt wilt."}
    targets = allowed

    language = "Antwoord in het Nederlands." if ctx.config.language == "nl" else "Answer in English."
    results, images = [], []
    for cam in targets[:MAX_CAMERAS_PER_QUESTION]:
        prompt = (
            f"Dit is een actueel beeld van camera '{cam.name}'. "
            f"{question or 'Beschrijf kort wat je ziet, vooral personen, voertuigen, dieren en pakketjes.'} "
            f"Wees feitelijk en beknopt. {language}"
        )
        try:
            snapshot = await ctx.ha.camera_snapshot(cam.entity_id)
        except Exception as err:  # noqa: BLE001
            results.append({"camera": cam.name, "error": f"geen beeld: {str(err)[:200]}"})
            continue
        images.append(cam.entity_id)  # the snapshot is worth showing even if the analysis fails
        try:
            description = await ctx.llm.describe_image(snapshot, prompt)
        except Exception as err:  # noqa: BLE001
            text = str(err).lower()
            reason = ("te weinig GPU-geheugen voor het beeldmodel" if "out of memory" in text
                      else "beeld te groot voor het beeldmodel" if "context size" in text
                      else str(err)[:200])
            results.append({"camera": cam.name, "error": f"beeldanalyse mislukt: {reason}"})
            continue
        results.append({"camera": cam.name, "description": description})
        ctx.db.audit("analyze_camera", {"entity_id": cam.entity_id, "question": question}, "executed",
                     description[:500], ctx.conversation_id)
    return {"results": results, "images": images}


# --- Registry ------------------------------------------------------------------

ENTITY_IDS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Lijst met entity_ids, bijv. ['light.woonkamer']",
}

TOOLS = [
    Tool(
        "get_home_overview",
        "Overzicht van het huis: ruimtes, wie thuis is, lampen aan, open ramen/deuren, klimaat, "
        "temperatuur/vocht/CO₂ per ruimte (room_readings), camera's.",
        {"type": "object", "properties": {}},
        get_home_overview,
    ),
    Tool(
        "search_entities",
        "Zoek entiteiten (apparaten/sensoren) op naam, ruimte, domein of device_class. "
        "Gebruik dit altijd als je de exacte entity_id niet kent.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Zoekwoorden, bijv. 'temperatuur slaapkamer'"},
                "area": {"type": "string", "description": "Ruimte, bijv. 'woonkamer'"},
                "domain": {"type": "string", "description": "Domein, bijv. light, sensor, climate, camera, cover"},
                "device_class": {"type": "string", "description": "Bijv. temperature, motion, window, door, power"},
                "limit": {"type": "integer", "description": "Max. aantal resultaten (standaard 15)"},
            },
        },
        search_entities,
    ),
    Tool(
        "get_state",
        "Actuele toestand en belangrijkste eigenschappen van één of meer entiteiten.",
        {"type": "object", "properties": {"entity_ids": ENTITY_IDS}, "required": ["entity_ids"]},
        get_state,
    ),
    Tool(
        "get_history",
        "Geschiedenis van één entiteit over een periode. Getallen: min/max/gemiddelde. Aan/uit, open/dicht, "
        "thuis/weg: het exacte aantal keer (count), de tijdstippen en hoe lang (total_duration). Voor 'hoe vaak', "
        "'wanneer', 'hoe lang stond X aan/open', 'wanneer kwam Y thuis'.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "period": {"type": "string", "description": "today, yesterday, day_before_yesterday, this_week, "
                           "last_7_days, night, last_30_days, een weekdag ('zaterdag') of een datum (JJJJ-MM-DD)"},
                "hours": {"type": "number", "description": "Of: aantal uur terug"},
                "start": {"type": "string", "description": "Of: begin (JJJJ-MM-DDTHH:MM of HH:MM vandaag)"},
                "end": {"type": "string", "description": "Einde bij start (standaard nu)"},
                "state": {"type": "string", "description": "Te tellen toestand, bijv. 'on', 'open', 'home', 'weg' "
                          "(standaard aan/open/thuis)"},
                "today": {"type": "boolean", "description": "true = vanaf middernacht vandaag"},
            },
            "required": ["entity_id"],
        },
        get_history,
    ),
    Tool(
        "get_detections",
        "Hoe vaak en wanneer camera's/sensoren iets detecteerden: personen, voertuigen, dieren, pakketjes, "
        "aanbellen of beweging, per plek over een periode. Telt exact (ook dubbele streams Duidelijk/Vloeiend "
        "als één camera). Gebruik dit voor 'hoe vaak/wanneer was er iemand bij X', 'is er aangebeld'.",
        {
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "person, vehicle, animal, package, doorbell, motion of "
                         "leeg voor alles"},
                "where": {"type": "string", "description": "Plek, ruimte of camera, bijv. 'voordeur' (leeg = overal)"},
                "period": {"type": "string", "description": "today (standaard), yesterday, this_week, last_7_days, "
                           "night, een weekdag of een datum (JJJJ-MM-DD)"},
                "hours": {"type": "number", "description": "Of: aantal uur terug"},
                "start": {"type": "string", "description": "Of: begin (JJJJ-MM-DDTHH:MM of HH:MM vandaag)"},
                "end": {"type": "string", "description": "Einde bij start (standaard nu)"},
            },
        },
        get_detections,
    ),
    Tool(
        "get_forecast",
        "Weersverwachting (dagelijks of per uur). Gebruik voor vragen over regen, temperatuur of wind later.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "weather.* entiteit (optioneel)"},
                "kind": {"type": "string", "enum": ["daily", "hourly"]},
            },
        },
        get_forecast,
    ),
    Tool(
        "control_light",
        "Lampen aan/uit zetten, dimmen of van kleur veranderen.",
        {
            "type": "object",
            "properties": {
                "entity_ids": ENTITY_IDS,
                "action": {"type": "string", "enum": ["on", "off", "toggle"]},
                "brightness_pct": {"type": "integer", "description": "Helderheid 0-100"},
                "color_name": {"type": "string", "description": "Kleur, bijv. red, blue, warmwhite"},
                "color_temp_kelvin": {"type": "integer", "description": "Kleurtemperatuur, bijv. 2700 (warm) - 6500 (koel)"},
            },
            "required": ["entity_ids", "action"],
        },
        control_light,
    ),
    Tool(
        "set_climate",
        "Thermostaat/airco instellen: doeltemperatuur, hvac_mode (heat, cool, auto, off) of preset.",
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "temperature": {"type": "number"},
                "hvac_mode": {"type": "string"},
                "preset_mode": {"type": "string"},
            },
            "required": ["entity_id"],
        },
        set_climate,
    ),
    Tool(
        "turn_on_off",
        "Willekeurige entiteiten aan/uit zetten (schakelaars, ventilatoren, media, scenes, scripts, automations).",
        {
            "type": "object",
            "properties": {"entity_ids": ENTITY_IDS, "action": {"type": "string", "enum": ["on", "off", "toggle"]}},
            "required": ["entity_ids", "action"],
        },
        turn_on_off,
    ),
    Tool(
        "call_service",
        "Een willekeurige Home Assistant service aanroepen voor zaken waar geen andere tool voor is, "
        "bijv. cover.open_cover, media_player.volume_set, vacuum.start, notify.mobile_app_x.",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_ids": ENTITY_IDS,
                "data": {"type": "object", "description": "Extra service data, bijv. {'position': 50}"},
            },
            "required": ["domain", "service"],
        },
        call_service,
    ),
    Tool(
        "analyze_camera",
        "Bekijk het actuele camerabeeld en beantwoord een vraag erover. Geef een ruimte (area, bijv. "
        "'achtertuin') om alle camera's daar te bekijken, of een entity_id voor één camera.",
        {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "Ruimte of plek, bijv. 'achtertuin', 'voordeur'"},
                "entity_id": {"type": "string", "description": "camera.* entiteit (optioneel)"},
                "question": {"type": "string", "description": "Wat wil je weten, bijv. 'Staat er een pakketje?'"},
            },
        },
        analyze_camera,
    ),
]


AUTONOMOUS_TOOL_NAMES = {
    "search_entities", "get_state", "get_history", "get_detections", "get_forecast", "analyze_camera",
    "control_light", "set_climate", "turn_on_off", "call_service",
}
AUTONOMOUS_AUTOMATION_TOOLS = {"list_automations", "get_automation", "related_automations"}


def build_registry() -> ToolRegistry:
    from ..comfort import comfort_tools  # comfort imports the agent package, which imports tools
    from ..doctor import DOCTOR_TOOLS
    from ..scheduled import SCHEDULE_TOOLS
    from ..watcher.thresholds import WATCH_TOOLS

    return ToolRegistry(TOOLS + AUTOMATION_TOOLS + HOUSE_RULE_TOOLS + WATCH_TOOLS + SCHEDULE_TOOLS + comfort_tools()
                        + CONTEXT_TOOLS + DOCTOR_TOOLS)


def build_autonomous_registry() -> ToolRegistry:
    """A smaller tool set for the heartbeat: fewer tokens and fewer ways for a small model to go wrong."""
    return ToolRegistry([t for t in TOOLS if t.name in AUTONOMOUS_TOOL_NAMES]
                        + [t for t in AUTOMATION_TOOLS if t.name in AUTONOMOUS_AUTOMATION_TOOLS] + AGENT_TOOLS)
