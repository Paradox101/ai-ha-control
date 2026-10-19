"""Editing the add-on options from the web UI.

Changes are applied immediately (everything reads the shared Config) and stored through the
Supervisor, so the add-on configuration page in Home Assistant shows the same values.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .autonomy import LEVEL_ORDER, MAX_LEVEL, parse_levels
from .config import Config, supervisor_token

if TYPE_CHECKING:
    from .ha.index import EntityIndex
    from .llm.ollama import OllamaClient

_LOGGER = logging.getLogger(__name__)

INTERNAL = {"ha_url", "ha_token", "data_dir", "port", "api_key"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DOMAIN_RE = re.compile(r"^[a-z_]+(\.[a-z0-9_]+)?$")
PERSON_NOTIFY_RE = re.compile(r"^person\.[a-z0-9_]+=[a-z_]+\.[a-z0-9_]+$")

AUTONOMY_DOMAINS = [
    ("light", "Verlichting"), ("climate", "Verwarming/airco"), ("fan", "Ventilatoren"), ("switch", "Schakelaars"),
    ("media_player", "Media"), ("cover", "Rolluiken/garage"), ("lock", "Sloten"),
    ("alarm_control_panel", "Alarm"), ("valve", "Kranen"), ("vacuum", "Stofzuiger"), ("scene", "Scènes"),
    ("script", "Scripts"), ("humidifier", "Luchtbevochtiger"), ("water_heater", "Boiler"),
]


COMBINED = ("scene_script", "valve_switch", "fan_switch", "fan_switch_script")  # entity pickers over several domains
# Pickers that are no plain domain: which entities they list.
SPECIAL_PICKERS = {
    "temperature": lambda e: e.domain == "sensor" and e.device_class == "temperature",
    "mode_helper": lambda e: e.domain in ("input_text", "input_select"),
    "presence": lambda e: e.domain in ("binary_sensor", "device_tracker", "input_boolean")
    and (e.domain != "binary_sensor" or e.device_class in ("presence", "occupancy", "running")),
}


def _f(key: str, label: str, kind: str, help: str = "", **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": kind, "help": help, **extra}


SECTIONS: list[dict[str, Any]] = [
    {"id": "ai", "title": "AI en modellen", "icon": "🧠", "fields": [
        _f("ollama_url", "Ollama-adres", "url", "Waar Ollama draait, bijv. http://192.168.1.10:11434", test="ollama"),
        _f("model", "Taalmodel", "model", "Voor gesprekken en bediening. Moet tool-calling ondersteunen.",
           capability="tools"),
        _f("triage_model", "Model voor de hartslag", "model",
           "Klein en snel model voor de autonome controles. Leeg = het taalmodel.", capability="tools",
           optional=True),
        _f("vision_model", "Beeldmodel", "model", "Voor camerabeelden. Moet beelden (vision) ondersteunen.",
           capability="vision"),
        _f("context_size", "Contextgrootte", "number", "Hoeveel tekst het model tegelijk ziet. Meer = meer VRAM.",
           min=2048, max=131072, step=1024, unit="tokens"),
        _f("think", "Denkmodus", "toggle", "Eerst laten nadenken: soms slimmer, altijd trager."),
        _f("keep_alive", "Model geladen houden", "text", "Bijv. 30m of 2h. Daarna geeft Ollama het VRAM vrij."),
        _f("language", "Taal", "segmented", options=[["nl", "Nederlands"], ["en", "English"]]),
    ]},
    {"id": "safety", "title": "Veiligheid", "icon": "🛡️", "fields": [
        _f("dry_run", "Testmodus", "toggle",
           "Aan: de AI vertelt wat hij zou doen, maar voert niets uit. Zet uit als je tevreden bent.",
           emphasis=True),
        _f("confirm_domains", "Altijd eerst vragen bij", "chips",
           "Acties op deze domeinen vragen altijd bevestiging, ook in de chat.", suggestions="domains"),
        _f("deny_domains", "Nooit toegestaan", "chips", "Domeinen waar de AI nooit iets mag doen.",
           suggestions="domains"),
        _f("deny_services", "Verboden acties", "chips", "Specifieke services, bijv. homeassistant.restart."),
        _f("only_exposed_entities", "Alleen blootgestelde entiteiten", "toggle",
           "Beperk de AI tot wat je bij Assist hebt blootgesteld."),
        _f("max_tool_calls", "Max. stappen per vraag", "number", "", min=1, max=30, step=1),
    ]},
    {"id": "autonomy", "title": "Autonome agent", "icon": "🤖", "fields": [
        _f("autonomous", "Autonome agent", "toggle",
           "De AI kijkt zelf regelmatig rond, opent incidenten en volgt ze op.", emphasis=True),
        _f("autonomy", "Wat mag de agent zelf?", "autonomy",
           "Kijken: alleen melden. Schaduw: vastleggen wat hij zou doen, zodat je het kunt beoordelen. "
           "Vragen: melding met knop. Zelf doen: direct, met 'ongedaan maken'. Sloten en alarm nooit zelf.",
           domains=AUTONOMY_DOMAINS, capped=MAX_LEVEL),
        _f("autonomous_scenes", "Scènes en scripts die hij zelf mag starten", "entities",
           "Alle andere scènes en scripts vraagt hij eerst. Scènes die deuren of het alarm raken nooit.",
           domain="scene_script"),
        _f("override_minutes", "Na handmatige aanpassing afblijven", "number",
           "Zet iemand zelf een lamp of de thermostaat anders, dan laat de agent dat apparaat zo lang met rust.",
           min=0, max=1440, step=15, unit="min"),
        _f("kill_switch", "Noodknop", "entity",
           "Staat deze schakelaar uit, dan doet de agent niets meer zelf (alleen melden).",
           domain="input_boolean", optional=True),
        _f("max_climate_change", "Max. temperatuurwijziging", "number", "Hoeveel graden de agent zelf mag bijstellen.",
           min=0, max=10, step=0.5, unit="°C"),
        _f("max_autonomous_actions_per_hour", "Max. eigen acties per uur", "number", "", min=0, max=100, step=1),
        _f("heartbeat_minutes", "Rondkijken elke", "number", "", min=1, max=120, step=1, unit="min"),
    ]},
    {"id": "comfort", "title": "Comfort", "icon": "🌡️", "fields": [
        _f("comfort", "Comfortprofielen uitvoeren", "toggle",
           "Temperatuur, schemerverlichting, ventilatie en zonwering per ruimte volgens de profielen "
           "(tabblad Comfort). Volgt de autonomie-instellingen hierboven."),
        _f("preheat_distance_km", "Voorverwarmen vanaf", "number",
           "Iemand komt naar huis (proximity-integratie) en is dichterbij dan dit. Een zone met 'voorloop' of "
           "'thuiskomst' in de naam telt ook.", min=1, max=100, step=1, unit="km"),
        _f("outdoor_sensor", "Buitentemperatuur", "entity",
           "Sensor buiten (voor cv-backup en zonwering). Leeg = de weersverwachting.", domain="temperature",
           optional=True),
        _f("ventilation_entity", "Centrale ventilatie", "entity",
           "Bijv. de WTW of mechanische ventilatie: gaat aan als het in een ruimte benauwd is. Leeg en er is een "
           "ventilatie-unit: die laat de agent aan je eigen automatiseringen over.", domain="fan_switch_script",
           optional=True),
        _f("shade_max_wind", "Geen zonwering boven", "number",
           "Zonneschermen gaan niet uit bij meer wind of bij vorst.", min=0, max=150, step=1, unit="km/u"),
    ]},
    {"id": "playbooks", "title": "Noodgevallen", "icon": "🧯", "fields": [
        _f("leak_valve", "Hoofdkraan bij lekkage", "entity",
           "Wordt direct dichtgedaan als een lekkagesensor afgaat.", domain="valve_switch", optional=True),
        _f("emergency_lights", "Alle lampen aan bij rook/gas/CO", "toggle", "Zodat iedereen de weg naar buiten ziet."),
        _f("emergency_fans_off", "Ventilatie uit bij rook", "entities", "", domain="fan_switch"),
    ]},
    {"id": "notifications", "title": "Meldingen", "icon": "🔔", "fields": [
        _f("notify_service", "Telefoon", "select",
           "Waar meldingen heen gaan. Leeg = alleen in Home Assistant (zonder knoppen en foto).",
           choices="notify", optional=True),
        _f("quiet_hours", "Stille uren", "timerange",
           "Geen gewone pushmeldingen in deze periode. Alarmen en de deurbel komen altijd door.", optional=True),
        _f("daily_summary_time", "Ochtendsamenvatting", "time", "", optional=True),
        _f("proactive", "Proactieve meldingen", "toggle", "Rook, lekkage, camera's, open ramen, onbeveiligd huis."),
        _f("tts_entity", "Spraakdienst", "entity", "Om meldingen uit te spreken, bijv. Piper.",
           domain="tts", optional=True),
        _f("announce_players", "Speakers", "entities", "Waar de deurbel en alarmen worden omgeroepen.",
           domain="media_player"),
        _f("person_notify", "Per persoon", "people",
           "Telefoon per bewoner. De deurbel en comfortmeldingen gaan dan alleen naar wie thuis is; "
           "alarmen naar iedereen. Leeg = alles naar de telefoon hierboven."),
        _f("digest_time", "Avondoverzicht", "time",
           "Niet-dringende meldingen (afwijkingen, energie, apparaten) gebundeld in één melding.", optional=True),
        _f("llm_down_minutes", "Melden als de AI-server weg is na", "number", "", min=5, max=1440, step=5,
           unit="min"),
        _f("bedtime_check", "Nachtronde", "toggle",
           "Bij het slapengaan één melding met wat nog open of aan staat. Sloten en garage alleen als vraag."),
        _f("appliance_alerts", "Apparaat klaar", "toggle",
           "Melding als de wasmachine, droger of vaatwasser klaar is (via een stroommeter met die naam)."),
        _f("appliance_quiet", "Apparaten met een eigen melding", "chips",
           "Bijv. wasmachine of vaatwasser: je eigen automatisering meldt al dat hij klaar is.", free=True),
        _f("leak_flow_hours", "Waarschuwen als water blijft lopen na", "number",
           "Watermeter die zo lang zonder pauze telt ('s nachts en als je weg bent; overdag 3× zo lang). 0 = uit.",
           min=0, max=24, step=0.5, unit="uur"),
        _f("health_report", "Huisdokter", "toggle",
           "Maandagochtend een overzicht van kapotte automatiseringen, updates en dode apparaten (alleen als er iets is)."),
        _f("offline_house_only", "Offline-meldingen alleen voor het huis", "toggle",
           "Alleen apparaten in een ruimte, sloten, camera's en thermostaten. Containers en serverstatistieken niet."),
        _f("offline_ignore_areas", "Geen offline-meldingen voor ruimtes", "chips", "Bijv. Server.", free=True,
           suggestions="areas"),
        _f("presence_simulation", "Aanwezigheid nabootsen op vakantie", "toggle",
           "In de huismodus vakantie 's avonds lampen aan en uit zoals het huis normaal gebruikt wordt. Alleen lampen."),
        _f("escalate_minutes", "Alarm herhalen als niemand 'Gezien' drukt na", "number",
           "Naar iedereen en omroepen op de speakers, maximaal 3 keer. 0 = uit.", min=0, max=60, step=1, unit="min"),
    ]},
    {"id": "cameras", "title": "Camera's", "icon": "📷", "fields": [
        _f("camera_alerts", "Bij detectie het beeld bekijken", "segmented",
           "Weg = alleen als niemand thuis is. Aanbellen wordt altijd gemeld.",
           options=[["off", "Uit"], ["away", "Als ik weg ben"], ["always", "Altijd"]]),
        _f("camera_profiles", "Wat wil je per camera horen?", "cameras",
           "Voordeur: alleen als iemand blijft rondhangen. Privé: elke persoon, kritiek als het alarm aan staat. "
           "Straat: alleen iets verdachts. Voorbijgangers en geparkeerde auto's worden nooit gemeld.",
           options=[["auto", "Automatisch"], ["door", "🚪 Voordeur"], ["private", "🔒 Privé"],
                    ["street", "🛣️ Straat"], ["off", "Negeren"]]),
        _f("camera_patrol", "Patrouille", "segmented",
           "Regelmatig alle camera's vergelijken met het vorige beeld.",
           options=[["off", "Uit"], ["away", "Weg of slapen"], ["always", "Altijd"]]),
        _f("patrol_minutes", "Patrouille elke", "number", "", min=5, max=240, step=5, unit="min"),
        _f("snapshot_retention_days", "Snapshots bewaren", "number", "", min=1, max=90, step=1, unit="dagen"),
    ]},
    {"id": "house", "title": "Huis", "icon": "🏠", "fields": [
        _f("sleep_hours", "Slaaptijden", "timerange", "Bepaalt de huismodus 'slapen'.", optional=True),
        _f("mode_entity", "Eigen huismodus-helper", "entity",
           "Houd je de huismodus zelf bij (bijv. input_text.actieve_scene met afwezig/slapen/thuis), dan volgt de "
           "agent die. Leeg = zelf zoeken.", domain="mode_helper", optional=True),
        _f("presence_extra", "Ook 'iemand thuis'", "entities",
           "Naast personen: bijv. de Tado-aanwezigheid of een sleutelhanger-tag.", domain="presence"),
        _f("calendar_context", "Agenda gebruiken", "toggle",
           "Afspraken in de ochtendsamenvatting; een lopende afspraak met 'vakantie' of 'gasten' zet de huismodus."),
        _f("energy_context", "Energieprijzen gebruiken", "toggle",
           "Goedkoopste stroommoment en zonneverwachting in de ochtendsamenvatting en op vraag."),
        _f("open_window_minutes", "Raam open + verwarming", "number", "Waarschuwen na", min=1, max=240, step=1,
           unit="min"),
        _f("unlocked_away_minutes", "Slot/garage open terwijl weg", "number", "Waarschuwen na", min=1, max=240,
           step=1, unit="min"),
        _f("routine_learning", "Routines leren", "toggle", "Herken terugkerende handelingen en stel "
           "automatiseringen voor."),
        _f("log_level", "Logniveau", "segmented",
           options=[["debug", "Debug"], ["info", "Info"], ["warning", "Waarschuwing"], ["error", "Fout"]]),
    ]},
]

FIELD_INDEX = {f["key"]: f for section in SECTIONS for f in section["fields"]}


def option_keys() -> list[str]:
    return [f.name for f in fields(Config) if f.name not in INTERNAL]


def current_values(config: Config) -> dict[str, Any]:
    return {key: getattr(config, key) for key in option_keys()}


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def validate(config: Config, changes: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check submitted values; raises SettingsError with a message per field."""
    clean, errors = {}, {}
    for key, value in changes.items():
        if key not in FIELD_INDEX:
            continue
        spec, current = FIELD_INDEX[key], getattr(config, key)
        try:
            clean[key] = _coerce(spec, current, value)
        except ValueError as err:
            errors[key] = str(err)
    if errors:
        raise SettingsError(errors)
    return clean


def _coerce(spec: dict[str, Any], current: Any, value: Any) -> Any:
    kind = spec["type"]
    if kind == "toggle":
        if not isinstance(value, bool):
            raise ValueError("moet aan of uit zijn")
        return value
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("geen geldig getal") from None
        if not spec.get("min", -1e18) <= number <= spec.get("max", 1e18):
            raise ValueError(f"moet tussen {spec.get('min')} en {spec.get('max')} liggen")
        return int(number) if isinstance(current, int) and not isinstance(current, bool) else number
    if kind in ("chips", "entities"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("ongeldige lijst")
        items = [v.strip() for v in value if v.strip()]
        bad = [] if spec.get("free") else [v for v in items if not DOMAIN_RE.match(v)]
        if bad:
            raise ValueError(f"ongeldig: {', '.join(bad)}")
        return items
    if kind == "people":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("ongeldige lijst")
        items = [v.strip() for v in value if v.strip()]
        bad = [v for v in items if not PERSON_NOTIFY_RE.match(v)]
        if bad:
            raise ValueError(f"gebruik person.naam=notify.dienst: {', '.join(bad)}")
        return items
    if kind == "cameras":
        if not isinstance(value, list):
            raise ValueError("ongeldige lijst")
        allowed = {o[0] for o in spec["options"]}
        items = [str(v) for v in value]
        bad = [v for v in items if not re.match(r"^camera\.[a-z0-9_]+=", v) or v.split("=", 1)[1] not in allowed]
        if bad:
            raise ValueError(f"ongeldig: {', '.join(bad)}")
        return [v for v in items if not v.endswith("=auto")]
    if kind == "autonomy":
        if not isinstance(value, list):
            raise ValueError("ongeldige lijst")
        levels = parse_levels([str(v) for v in value])
        if len(levels) != len(value):
            raise ValueError("gebruik domein=observe|ask|act")
        return [f"{d}={lvl}" for d, lvl in levels.items()]
    value = "" if value is None else str(value).strip()
    if not value and spec.get("optional"):
        return ""
    if kind == "url" and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("moet beginnen met http:// of https://")
    if kind == "time" and not TIME_RE.match(value):
        raise ValueError("gebruik UU:MM")
    if kind == "timerange":
        parts = [p.strip() for p in value.split("-")]
        if len(parts) != 2 or not all(TIME_RE.match(p) for p in parts):
            raise ValueError("gebruik UU:MM-UU:MM")
        return f"{parts[0]}-{parts[1]}"
    if kind == "segmented" and value not in [o[0] for o in spec["options"]]:
        raise ValueError("ongeldige keuze")
    if kind in ("entity", "select") and value and not DOMAIN_RE.match(value):
        raise ValueError("ongeldige entiteit")
    if not value:
        raise ValueError("mag niet leeg zijn")
    return value


def apply(config: Config, changes: dict[str, Any]) -> None:
    for key, value in changes.items():
        setattr(config, key, value)
    if "log_level" in changes:
        logging.getLogger().setLevel(getattr(logging, config.log_level.upper(), logging.INFO))


async def persist(session: aiohttp.ClientSession, config: Config) -> str | None:
    """Store all options; returns a warning when they only apply until the next restart."""
    values = current_values(config)
    values["api_key"] = config.api_key
    token = supervisor_token()
    if not token:
        path = Path(os.environ.get("OPTIONS_FILE", Path(config.data_dir) / "options.json"))
        try:
            path.write_text(json.dumps(values, indent=2, ensure_ascii=False))
        except OSError as err:
            return f"niet opgeslagen: {err}"
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get("http://supervisor/addons/self/info", headers=headers) as resp:
            info = await resp.json()
        options = {**(info.get("data", {}).get("options") or {}), **values}
        async with session.post("http://supervisor/addons/self/options", json={"options": options},
                                headers=headers) as resp:
            if resp.status >= 400:
                body = await resp.json(content_type=None)
                return f"Supervisor weigerde de opties: {body.get('message', resp.status)}"
    except (aiohttp.ClientError, ValueError) as err:
        return f"opslaan via de Supervisor mislukt: {err}"
    return None


async def choices(index: EntityIndex, llm: OllamaClient) -> dict[str, Any]:
    """Dynamic options for dropdowns: installed models, notify services, entities."""

    def entities(domain: str) -> list[dict[str, str]]:
        domains = set(domain.split("_")) if domain in COMBINED else {domain}
        pick = SPECIAL_PICKERS.get(domain, lambda e: e.domain in domains)
        return sorted(({"value": e.entity_id, "label": e.name + (f" · {e.area}" if e.area and domain in SPECIAL_PICKERS
                                                                  else "")}
                       for e in index.entities.values() if pick(e)), key=lambda e: e["label"].lower())

    return {
        "models": await llm.models(),
        "notify": [{"value": f"notify.{name}", "label": name.replace("mobile_app_", "📱 ")}
                   for name in sorted(index.services.get("notify", {})) if name not in ("notify", "send_message")],
        "domains": sorted({e.domain for e in index.entities.values()} | {d for d, _ in AUTONOMY_DOMAINS}
                          | {"shell_command", "hassio", "recorder", "python_script"}),
        "entities": {d: entities(d) for d in ("input_boolean", "tts", "media_player", "person", *COMBINED,
                                              *SPECIAL_PICKERS)},
        "areas": sorted(index.areas.values()),
        "levels": list(LEVEL_ORDER),
        "cameras": sorted(({"value": c.entity_id, "label": index.camera_label(c),
                            "guess": index.camera_profile(c, [])} for c in index.cameras(limit=100)),
                          key=lambda c: c["label"].lower()),
    }
