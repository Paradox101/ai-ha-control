"""What a real house looks like to the rules: which sensors describe a room, which cameras are real cameras,
what the outdoor temperature is, which helpers pause the agent. Only reads the index, never acts.

Real installations are noisy: a fridge sensor in the kitchen, a thermostat's setpoint exposed as a temperature
sensor, mail-delivery "cameras", a robot vacuum's map. These helpers keep that noise out of comfort,
cameras and the energy context.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .ha.index import CAR_WORDS, DETECTION_CLASSES, EntityIndex, EntityInfo, normalize

if TYPE_CHECKING:
    from .config import Config

AIR_CLASSES = ("temperature", "humidity", "carbon_dioxide")
# Sensors in a room that do not tell how warm/humid the room itself is.
NOT_ROOM_WORDS = {
    "target", "setpoint", "dew", "dauwpunt", "surface", "koelkast", "vriezer", "fridge", "freezer", "inverter",
    "omvormer", "heatsink", "laadpaal", "charger", "cpu", "gpu", "core", "chip", "boiler", "itho", "cve", "wtw",
    "outdoor", "buiten", "mold", "schimmel", "max", "min", "minimum", "maximum", "gemiddelde", "average",
    "goodwe", "nas", "server", "ups", "afvoer", "exhaust",
    # Computer hardware (Glances, lm-sensors): chipset, board and drive sensors, not the air of a room.
    "acpitz", "composite", "tctl", "tdie", "systin", "cputin", "peci", "package", "nvme", "pch", "spd5118",
    "auxtin0", "auxtin1", "auxtin2", "auxtin3", "auxtin4", "auxtin5", "mt7921", "r8169", "iwlwifi", "coretemp",
}
# Areas with computers and network gear, not people: their sensors describe the hardware.
NOT_ROOM_AREAS = {"server", "serverkast", "serverruimte", "serverrack", "netwerkkast", "nas", "datacenter"}
IP_NAME = re.compile(r"^\d{1,3}(\.\d{1,3}){3}\b")
# A phone's or Tado's geofencing ("Tado CE Hub Geofencing", binary_sensor.home): someone is home, nobody moves.
GEOFENCE_WORDS = {"geofencing", "geofence"}
# Cameras that are no camera: mail deliveries, rain radar, a vacuum's map.
PSEUDO_CAMERA_WORDS = {"imap", "mail", "delivery", "buienradar", "radar", "map", "kaart"}
# Cameras inside rooms where people sleep or wash: never look at them by themselves.
PRIVATE_ROOM_WORDS = {"baby", "babycam", "babyfoon", "babyphone", "nursery", "kinderkamer", "slaapkamer", "bedroom",
                      "badkamer", "bathroom", "vm65", "vm75", "nanit", "owlet", "babykamer", "logeerkamer", "douche",
                      "shower", "toilet", "wc"}
VENTILATION_WORDS = {"ventilatie", "ventilation", "itho", "cve", "wtw", "mv", "zehnder", "orcon", "brink"}
AWNING_WORDS = {"zonnescherm", "markies", "knikarm", "awning", "screen"}
ARRIVING_ZONE_WORDS = {"voorloop", "thuiskomst", "bijna", "onderweg", "approach", "nearby"}
MODE_HELPER_WORDS = ({"actieve", "scene"}, {"huismodus"}, {"house", "mode"}, {"home", "mode"})
CURRENCY_HINTS = ("€", "eur", "ct", "cent", "$", "usd", "£", "gbp", "kr", "sek", "nok", "dkk", "chf")
WASTE_WORDS = ("afvalwijzer", "afval", "waste", "garbage", "container", "kliko")


def words(ent: EntityInfo) -> set[str]:
    """Words of the entity id, the name and the device: what the entity is."""
    text = f"{ent.entity_id.split('.', 1)[1].replace('_', ' ')} {ent.name} {ent.device or ''}"
    return set(normalize(text).split())


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Rooms --------------------------------------------------------------------------------

def is_hardware(ent: EntityInfo) -> bool:
    """A server, router or container statistic: in a server area or on a device named after its IP address."""
    return bool(set(normalize(ent.area or "").split()) & NOT_ROOM_AREAS) or bool(IP_NAME.match(ent.device or "")) \
        or bool(IP_NAME.match(ent.name or ""))


def is_room_sensor(ent: EntityInfo) -> bool:
    """A temperature/humidity/CO₂ sensor that measures the air of the room it is in."""
    if ent.domain != "sensor" or ent.device_class not in AIR_CLASSES or is_hardware(ent):
        return False
    return not words(ent) & NOT_ROOM_WORDS


def is_geofence(ent: EntityInfo) -> bool:
    return bool(words(ent) & GEOFENCE_WORDS)


def room_air(index: EntityIndex) -> dict[str, dict[str, float]]:
    """Per area the median of the room sensors (one odd sensor cannot drag the room off)."""
    values: dict[str, dict[str, list[float]]] = {}
    for ent in index.entities.values():
        if not ent.area or not is_room_sensor(ent):
            continue
        value = _number(ent.state)
        if value is None:
            continue
        values.setdefault(ent.area, {}).setdefault(ent.device_class, []).append(value)
    return {area: {k: statistics.median(v) for k, v in by_class.items()} for area, by_class in values.items()}


def room_readings(index: EntityIndex, profiles: list[dict[str, Any]] | tuple = ()) -> dict[str, dict[str, float]]:
    """What each room measures, for chat and overview: the room median, a comfort profile's chosen temperature
    sensor wins, a thermostat's own reading fills a room without sensors. Rooms without any reading are left out."""
    result = {area: dict(v) for area, v in room_air(index).items()}
    for profile in profiles:
        value = sensor_value(index, profile.get("temperature_sensor"))
        if value is not None and profile.get("area"):
            result.setdefault(profile["area"], {})["temperature"] = value
    current: dict[str, list[float]] = {}
    for ent in index.entities.values():
        value = _number(ent.attributes.get("current_temperature")) if ent.domain == "climate" else None
        if ent.area and value is not None and ent.state not in ("unavailable", "unknown"):
            current.setdefault(ent.area, []).append(value)
    for area, values in current.items():
        result.setdefault(area, {}).setdefault("temperature", statistics.median(values))
    return {area: {k: round(v, 1) for k, v in values.items()} for area, values in sorted(result.items())}


def fmt_number(v: float, nl: bool = True) -> str:
    """19.7 → "19,7", 20.0 → "20"."""
    text = f"{v:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if nl else text


def readings_text(readings: dict[str, dict[str, float]], nl: bool = True) -> str:
    """"Woonkamer 19,9 °C (RV 55 %, CO₂ 650 ppm)" per room, rooms without a temperature after the others."""
    def fmt(v: float) -> str:
        return fmt_number(v, nl)

    parts = []
    for area, v in sorted(readings.items(), key=lambda kv: "temperature" not in kv[1]):
        extra = ([f"{'RV' if nl else 'RH'} {fmt(v['humidity'])} %"] if "humidity" in v else []) + (
            [f"CO₂ {v['carbon_dioxide']:.0f} ppm"] if "carbon_dioxide" in v else [])
        head = f"{area} {fmt(v['temperature'])} °C" if "temperature" in v else area
        parts.append(head + (f" ({', '.join(extra)})" if extra else ""))
    return "; ".join(parts)


def sensor_value(index: EntityIndex, entity_id: str | None) -> float | None:
    ent = index.get(entity_id) if entity_id else None
    return _number(ent.state) if ent else None


# --- Outside ----------------------------------------------------------------------------------

def weather(index: EntityIndex) -> EntityInfo | None:
    found = [e for e in index.entities.values() if e.domain == "weather" and e.state not in ("unavailable", "unknown")]
    return min(found, key=lambda e: e.entity_id) if found else None


def outside_temperature(index: EntityIndex, config: Config) -> float | None:
    """The configured outdoor sensor, otherwise the weather entity."""
    value = sensor_value(index, getattr(config, "outdoor_sensor", ""))
    if value is not None:
        return value
    w = weather(index)
    return _number(w.attributes.get("temperature")) if w else None


def wind_speed(index: EntityIndex) -> float | None:
    """km/h from the weather entity."""
    w = weather(index)
    if w is None:
        return None
    speed = _number(w.attributes.get("wind_speed"))
    if speed is None:
        return None
    unit = str(w.attributes.get("wind_speed_unit") or "km/h").lower()
    return speed * 3.6 if unit == "m/s" else speed


def is_awning(ent: EntityInfo) -> bool:
    return ent.domain == "cover" and (ent.device_class == "awning" or bool(words(ent) & AWNING_WORDS))


# --- Cameras ------------------------------------------------------------------------------

def is_pseudo_camera(ent: EntityInfo) -> bool:
    return ent.domain == "camera" and bool(words(ent) & PSEUDO_CAMERA_WORDS)


def is_private_room_camera(ent: EntityInfo) -> bool:
    """Dutch compounds count too: "ouderslaapkamer", "slaapkamers", "babykamercam"."""
    text = words(ent) | set(normalize(ent.area or "").split())
    return any(token == w or (len(w) >= 4 and (token.startswith(w) or token.endswith(w)))
               for token in text for w in PRIVATE_ROOM_WORDS)


# --- Helpers that pause the agent, house mode ------------------------------------------------

def paused(index: EntityIndex, entity_id: str | None, now: datetime | None = None) -> str:
    """Why a resident's pause helper holds the agent back ('' = not paused).

    input_boolean on, a timer running, or an input_datetime in the future ("gepauzeerd tot").
    """
    ent = index.get(entity_id) if entity_id else None
    if ent is None:
        return ""
    if ent.domain in ("input_boolean", "switch") and ent.state == "on":
        return f"{ent.name} staat aan"
    if ent.domain == "timer" and ent.state in ("active", "paused"):
        return f"{ent.name} loopt"
    if ent.domain == "input_datetime" and ent.attributes.get("has_date"):
        try:
            until = datetime.fromisoformat(ent.state)
        except ValueError:
            return ""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.astimezone()
        if until.tzinfo is None:
            until = until.replace(tzinfo=now.tzinfo)  # the helper is in Home Assistant's time zone, like `now`
        if until > now:
            return f"{ent.name} tot {until:%H:%M}"
    return ""


def mode_helper(index: EntityIndex, config: Config) -> EntityInfo | None:
    """The resident's own house mode helper (e.g. input_text.actieve_scene), set or found by name."""
    configured = getattr(config, "mode_entity", "")
    if configured:
        return index.get(configured)
    for ent in index.entities.values():
        if ent.domain in ("input_text", "input_select") and any(w <= words(ent) for w in MODE_HELPER_WORDS):
            return ent
    return None


def is_car(ent: EntityInfo) -> bool:
    """Part of a car. A camera's "Voertuig" detection (and the camera, its siren) only sees one."""
    if ent.domain in ("camera", "siren") or (ent.domain == "binary_sensor" and (
            ent.device_class in DETECTION_CLASSES or words(ent) & {"vehicle", "voertuig"})):
        return False
    # "auto" in an entity id is often "automatic" (a vacuum's auto_empty); in a name it is the car.
    named = set(normalize(f"{ent.name} {ent.device or ''}").split())
    return bool((words(ent) - {"auto"} | named) & CAR_WORDS)


UPS_WORDS = {"ups", "eaton", "apc", "nut", "cyberpower"}
CHARGE_HINTS = ("battery_state", "charging", "oplaad", "elvehcharg")


def rechargeable_devices(index: EntityIndex) -> set[str]:
    """Devices that charge themselves (phone, watch, vacuum, car, UPS): a low level is no battery to replace."""
    devices = set()
    for ent in index.entities.values():
        if not ent.device_id:
            continue
        if ent.domain in ("vacuum", "device_tracker", "lawn_mower") or is_car(ent) or words(ent) & UPS_WORDS \
                or any(h in ent.entity_id for h in CHARGE_HINTS):
            devices.add(ent.device_id)
    return devices


def label(ent: EntityInfo) -> str:
    """Name for messages: a car lock is not a house door."""
    return f"🚗 {ent.name} (auto)" if ent.domain == "lock" and is_car(ent) else ent.name


# State of charge, charging state, plug and range of an EV (FordPass, Tesla, Kia, ...). The 12 V battery of the
# same car is no state of charge: "accu 100 %" while the car is charging is the wrong answer.
CHARGER_WORDS = {"laadpaal", "charger", "wallbox", "evse", "alfen", "easee", "zaptec", "laadstation"}
LOW_VOLTAGE_WORDS = {"12v", "lv", "aux", "12", "accu12v", "starter", "startaccu"}
SOC_WORDS = {"soc", "laadtoestand", "laadniveau", "charge", "hvb", "ev", "elveh", "accu", "batterij", "battery"}
RANGE_WORDS = {"range", "bereik", "actieradius", "elveh", "distance"}
CHARGING_WORDS = {"elvehcharging", "oplaadstatus", "laadstatus", "charging"}
PLUG_WORDS = {"elvehplug", "stekker", "plug", "plugged", "cable"}
NOT_CHARGING = ("not", "niet", "complete", "completed", "target", "paused", "pause", "done", "finished", "stopped",
                "fault", "error", "geen", "volledig", "vol", "full", "geladen")
PLUGGED = {"connected", "plugged", "plugged in", "on", "ingeplugd", "verbonden"}
UNPLUGGED = {"disconnected", "not plugged in", "unplugged", "off", "uitgeplugd", "niet verbonden"}


def _usable(ent: EntityInfo) -> bool:
    return ent.state not in ("unknown", "unavailable", "")


def car_devices(index: EntityIndex) -> dict[str, list[EntityInfo]]:
    """Per car its entities, grouped by device (a charger is no car, even with "car" in a sensor's name)."""
    cars: dict[str, list[EntityInfo]] = {}
    for ent in index.entities.values():
        if not is_car(ent) or words(ent) & CHARGER_WORDS:
            continue
        cars.setdefault(ent.device or "Auto", []).append(ent)
    return {d: ents for d, ents in cars.items() if any(e.domain in ("lock", "sensor") for e in ents)}


def car_soc(ents: list[EntityInfo]) -> EntityInfo | None:
    """The EV's state of charge in %, never the 12 V battery."""
    found = []
    for e in ents:
        if e.domain != "sensor" or not _usable(e) or _number(e.state) is None or (e.unit or "%") != "%":
            continue
        w = words(e)
        if w & LOW_VOLTAGE_WORDS or not (e.device_class == "battery" or w & {"soc", "laadtoestand", "laadniveau"}):
            continue
        rank = 0 if w & {"soc", "laadtoestand", "laadniveau", "hvb"} else 1 if w & SOC_WORDS else 2
        found.append((rank, e.entity_id, e))
    return min(found)[2] if found else None


def _car_range(ents: list[EntityInfo]) -> EntityInfo | None:
    found = [e for e in ents if e.domain == "sensor" and _usable(e) and _number(e.state) is not None
             and (e.unit or "").lower() in ("km", "mi") and words(e) & RANGE_WORDS
             and not words(e) & {"odometer", "kilometerteller", "fuel", "brandstof"}]
    return min(found, key=lambda e: e.entity_id) if found else None


def _charging(ents: list[EntityInfo]) -> bool | None:
    """Charging or not according to the car's own charging sensor; None without one."""
    for e in sorted(ents, key=lambda e: e.entity_id):
        if e.domain in ("sensor", "binary_sensor") and _usable(e) and words(e) & CHARGING_WORDS \
                and not words(e) & PLUG_WORDS:
            if e.domain == "binary_sensor":
                return e.state == "on"
            text = normalize(e.state)
            return any(w in text for w in ("charging", "in progress", "laden", "laadt")) and \
                not any(w in text.split() or text.startswith(w) for w in NOT_CHARGING)
    return None


def _plugged(ents: list[EntityInfo]) -> bool | None:
    for e in sorted(ents, key=lambda e: e.entity_id):
        if e.domain in ("sensor", "binary_sensor") and _usable(e) and words(e) & PLUG_WORDS \
                and not words(e) & CHARGING_WORDS:
            text = normalize(e.state).replace("_", " ")
            if text in UNPLUGGED or text.replace(" ", "") in {u.replace(" ", "") for u in UNPLUGGED}:
                return False
            if text in PLUGGED or text.replace(" ", "") in {p.replace(" ", "") for p in PLUGGED}:
                return True
    return None


def charger_power(index: EntityIndex) -> EntityInfo | None:
    """The charge point's total power (Alfen, Easee, ...): live proof the car is charging."""
    found = []
    for e in index.entities.values():
        w = words(e)
        if e.domain != "sensor" or e.device_class != "power" or not w & CHARGER_WORDS or _number(e.state) is None:
            continue
        if w & {"max", "min", "limit", "maximum", "minimum", "l1", "l2", "l3", "phase", "fase"}:
            continue
        found.append((0 if "total" in w or "totaal" in w else 1, e.entity_id, e))
    return min(found)[2] if found else None


def _watt(e: EntityInfo) -> float:
    value = _number(e.state) or 0.0
    return value * 1000 if (e.unit or "").lower() == "kw" else value


def car_facts(index: EntityIndex) -> list[dict[str, Any]]:
    """Per car what its sensors say right now; None where the car has no such sensor."""
    charger = charger_power(index)
    charger_w = _watt(charger) if charger is not None else None
    facts = []
    for device, ents in sorted(car_devices(index).items()):
        soc, rng = car_soc(ents), _car_range(ents)
        charging = _charging(ents)
        if charger_w is not None and charger_w > 100:
            charging = True
        locks = [e for e in ents if e.domain == "lock" and _usable(e)]
        facts.append({
            "device": device,
            "name": device.split(" (")[0] or device,
            "soc": float(soc.state) if soc is not None else None,
            "charging": charging,
            "charger_kw": charger_w / 1000 if charging and charger_w is not None and charger_w > 100 else None,
            "plugged": _plugged(ents),
            "range": (float(rng.state), rng.unit) if rng is not None else None,
            "lock": locks[0] if locks else None,
        })
    return facts


def car_status(index: EntityIndex, nl: bool = True) -> list[str]:
    """Per car one line with real numbers: state of charge, charging or not, range, plug, lock."""
    lines = []
    for f in car_facts(index):
        parts = []
        if f["soc"] is not None:
            parts.append(f"{'laadtoestand (EV-accu)' if nl else 'state of charge (EV battery)'} "
                         f"{fmt_number(f['soc'], nl)} %")
        if f["charging"]:
            text = "NU AAN HET LADEN" if nl else "CHARGING NOW"
            if f["charger_kw"]:
                text += f" ({'laadpaal' if nl else 'charger'} {fmt_number(f['charger_kw'], nl)} kW)"
            parts.append(text)
        elif f["charging"] is False:
            parts.append("laadt niet" if nl else "not charging")
        if f["plugged"] is not None and not f["charging"]:
            parts.append(("stekker erin" if nl else "plugged in") if f["plugged"] else
                         ("stekker eruit" if nl else "unplugged"))
        if f["range"]:
            parts.append(f"{'bereik' if nl else 'range'} {fmt_number(f['range'][0], nl)} {f['range'][1]}")
        if f["lock"] is not None:
            parts.append(f["lock"].readable_state if nl else f["lock"].state)
        if f["soc"] is None:
            parts.append("laadtoestand onbekend (geen sensor)" if nl else "state of charge unknown (no sensor)")
        lines.append(f"{f['device']}: {', '.join(parts)}")
    return lines


CAR_QUESTION_WORDS = CAR_WORDS | {"explorer", "wagen", "ev", "elektrische"}
CAR_CHARGE_WORDS = {"vol", "accu", "batterij", "battery", "laadtoestand", "laadniveau", "soc", "procent", "opgeladen",
                    "geladen", "laden", "laadt", "full", "charge", "charged", "charging", "leeg", "empty"}
CAR_RANGE_WORDS = {"bereik", "actieradius", "range", "km", "kilometer", "kilometers", "rijden", "ver"}
CAR_LOCK_WORDS = {"slot", "dicht", "vergrendeld", "afgesloten", "locked", "lock"}
QUESTION_START = {"hoe", "is", "wat", "hoeveel", "staat", "laadt", "heeft", "zit", "how", "what", "does", "has",
                  "kan", "ben", "zijn", "geef"}
# Commands and questions about the past go to the agent: "doe de auto op slot", "wanneer laadde de auto".
NOT_A_STATUS_QUESTION = {"doe", "zet", "start", "stop", "ontgrendel", "vergrendel", "open", "begin", "pauzeer",
                         "wanneer", "gisteren", "vandaag", "lang", "vaak", "keer", "when", "yesterday", "unlock",
                         "automatisering", "automation", "camera", "gezien", "oprit", "straat"}


def car_answer(index: EntityIndex, question: str, nl: bool = True) -> str | None:
    """A status question about the car ("hoe vol is de auto?") answered straight from its sensors: small models
    turned "75 %" into "volledig vol". None for anything else (commands, history, other devices)."""
    tokens = normalize(question).split()
    if not tokens or not set(tokens) & CAR_QUESTION_WORDS or set(tokens) & NOT_A_STATUS_QUESTION:
        return None
    if "?" not in question and tokens[0] not in QUESTION_START:
        return None
    asks_charge, asks_range = bool(set(tokens) & CAR_CHARGE_WORDS), bool(set(tokens) & CAR_RANGE_WORDS)
    asks_lock = bool(set(tokens) & CAR_LOCK_WORDS) or "op slot" in " ".join(tokens)
    if not (asks_charge or asks_range or asks_lock):
        return None
    facts = car_facts(index)
    if not facts:
        return None
    sentences = []
    for f in facts:
        name = ("De auto" if nl else "The car") if len(facts) == 1 else f["name"]
        if asks_lock:
            lock = f["lock"]
            if lock is None:
                sentences.append(f"Het slot van {name.lower() if len(facts) == 1 else name} is niet bekend." if nl
                                 else f"The lock of {name.lower()} is not known.")
            elif nl:
                sentences.append(f"{name} staat {lock.readable_state}.")
            else:
                sentences.append(f"{name} is {'locked' if lock.state == 'locked' else lock.state}.")
        if asks_charge:
            if f["soc"] is None:
                sentences.append("De laadtoestand van de auto is niet bekend." if nl
                                 else "The car's state of charge is not known.")
            else:
                soc = fmt_number(f["soc"], nl)
                head = (f"De accu is {soc} % vol" if nl else f"The battery is at {soc} %") \
                    if asks_lock or len(facts) == 1 else f"{name}: {soc} %"
                if f["charging"]:
                    tail = (" en de auto is nu aan het laden" if nl else " and it is charging now") + (
                        f" ({fmt_number(f['charger_kw'], nl)} kW)" if f["charger_kw"] else "")
                elif f["charging"] is False or f["plugged"] is False:
                    tail = " en de auto laadt nu niet" if nl else " and it is not charging"
                else:
                    tail = ""
                sentences.append(head + tail + ".")
        if asks_range or (asks_charge and f["range"] and not asks_lock):
            if f["range"]:
                sentences.append(f"{'Het bereik is' if nl else 'The range is'} "
                                 f"{fmt_number(f['range'][0], nl)} {f['range'][1]}.")
            elif asks_range:
                sentences.append("Het bereik is niet bekend." if nl else "The range is not known.")
    return " ".join(sentences)


def house_fan(index: EntityIndex) -> EntityInfo | None:
    """A central ventilation unit, when the house has one."""
    found = [e for e in index.entities.values() if e.domain == "fan" and words(e) & VENTILATION_WORDS
             and e.state != "unavailable"]
    return min(found, key=lambda e: e.entity_id) if found else None


def arriving_zone(index: EntityIndex) -> str:
    """Someone is in an 'almost home' zone (e.g. zone.voorloop_thuiskomst): the name of that zone."""
    zones = {normalize(z.name): z.name for z in index.entities.values()
             if z.domain == "zone" and set(normalize(z.name).split()) & ARRIVING_ZONE_WORDS}
    zones.update({normalize(z.entity_id.split(".", 1)[1].replace("_", " ")): z.name for z in index.entities.values()
                  if z.domain == "zone" and set(z.entity_id.split(".", 1)[1].split("_")) & ARRIVING_ZONE_WORDS})
    for person in index.entities.values():
        if person.domain == "person" and normalize(person.state) in zones:
            return f"{person.name} is in zone {zones[normalize(person.state)]}"
    return ""


# --- Energy -----------------------------------------------------------------------------------

def is_price_unit(unit: str | None) -> bool:
    text = (unit or "").replace(" ", "").lower()
    return text.endswith("/kwh") and any(c in text for c in CURRENCY_HINTS)


def grid_power(index: EntityIndex) -> EntityInfo | None:
    """Net power from the smart meter (W, negative = feeding back)."""
    found = []
    for ent in index.entities.values():
        if ent.domain != "sensor" or ent.device_class != "power" or _number(ent.state) is None:
            continue
        w = words(ent)
        if w & {"phase", "fase", "l1", "l2", "l3", "socket"} or not w & {"p1", "grid", "net", "dsmr", "meter"}:
            continue
        found.append((0 if "p1" in w else 1, ent.entity_id, ent))
    return min(found)[2] if found else None


def solar_power(index: EntityIndex) -> EntityInfo | None:
    found = []
    for ent in index.entities.values():
        if ent.domain != "sensor" or ent.device_class != "power" or _number(ent.state) is None:
            continue
        w = words(ent)
        if w & {"forecast", "geschatte", "estimated", "verwacht"} or w & {"pv1", "pv2", "string"}:
            continue
        if w & {"pv", "solar", "zonnepanelen", "opwek"}:
            found.append((ent.entity_id, ent))
    return min(found)[1] if found else None


def ups_on_battery(index: EntityIndex) -> EntityInfo | None:
    """A UPS (NUT, Eaton, APC) that runs on its battery: the mains are down."""
    for ent in index.entities.values():
        if ent.domain == "sensor" and "status" in ent.entity_id:
            state = ent.state.strip()
            if state.split(" ")[0] == "OB" or state.lower() in ("on battery", "op batterij"):
                return ent
        if ent.domain == "input_boolean" and {"ups"} <= words(ent) and words(ent) & {"batterij", "battery"} \
                and ent.state == "on":
            return ent
    return None


def ups_runtime_minutes(index: EntityIndex, ups: EntityInfo) -> int | None:
    for ent in index.entities.values():
        if ent.domain == "sensor" and ent.device_id and ent.device_id == ups.device_id and \
                ent.device_class == "duration" and words(ent) & {"runtime", "batterijduur", "looptijd"}:
            value = _number(ent.state)
            if value is not None:
                return int(value / 60) if (ent.unit or "s") == "s" else int(value)
    return None


def waste_today_tomorrow(index: EntityIndex) -> list[str]:
    """'Morgen: pmd' from afvalwijzer-like sensors (state is the type)."""
    lines = []
    for when, keys in (("Vandaag", ("today", "vandaag")), ("Morgen", ("tomorrow", "morgen"))):
        for ent in index.entities.values():
            if ent.domain != "sensor" or not any(w in ent.entity_id for w in WASTE_WORDS):
                continue
            if not set(ent.entity_id.split(".", 1)[1].split("_")) & set(keys):
                continue
            if ent.state.lower() not in ("geen", "none", "", "unknown", "unavailable"):
                lines.append(f"{when}: {ent.state}")
                break
    return lines


# --- Duplicates (for the export and the doctor) ---------------------------------------------

def duplicates(index: EntityIndex) -> list[tuple[EntityInfo, EntityInfo]]:
    """Entities named like another one with a _2/_3 suffix: usually left over after re-adding a device.

    Two devices that both work in different rooms and only share a name (a "Sirene" on each floor) are not.
    """
    by_id = index.entities
    pairs = []
    for ent in by_id.values():
        base, _, suffix = ent.entity_id.rpartition("_")
        if not (suffix.isdigit() and int(suffix) in range(2, 10) and base in by_id and by_id[base].name == ent.name):
            continue
        original = by_id[base]
        dead = {"unavailable", "unknown"}
        if original.area and ent.area and original.area != ent.area \
                and original.state not in dead and ent.state not in dead:
            continue
        pairs.append((original, ent))
    return sorted(pairs, key=lambda p: p[1].entity_id)
