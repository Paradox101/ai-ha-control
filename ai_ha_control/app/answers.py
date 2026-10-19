"""Status questions answered straight from Home Assistant, without the language model.

Small models drop numbers ("75 %" became "volledig vol"), leave lights out or repeat a wrong answer from earlier in
the conversation. For plain status questions (which lights are on, is a window open, how warm is it, who is home,
the car) the facts are in the index, so the answer is built from them. Only a question made up of known words gets
this treatment: anything else (a light by name, a command, the past) goes to the agent.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .ha.index import EntityIndex, EntityInfo, NON_ROOM_AREAS, normalize
from .house import QUESTION_START, car_answer, fmt_number, outside_temperature, readings_text, room_readings

STATUS_START = QUESTION_START | {"welke", "wie", "staan", "zijn", "waar", "brandt", "branden", "which", "who", "are",
                                 "is", "any", "graag", "geef", "noem", "toon", "give", "show", "list"}
FILLER = {
    "de", "het", "een", "er", "in", "op", "bij", "van", "nog", "nu", "dit", "moment", "momenteel", "allemaal",
    "alle", "al", "welke", "wat", "wie", "hoe", "is", "zijn", "staan", "staat", "zit", "zitten", "huis", "thuis",
    "binnen", "ergens", "overal", "graag", "geef", "me", "mij", "even", "eens", "kun", "kan", "je", "jij", "zeggen",
    "vertellen", "laat", "zien", "hoeveel", "the", "are", "is", "which", "any", "at", "house", "home",
    "there", "what", "how", "many", "of", "all", "still", "right", "now", "please", "tell", "me", "show", "it",
    "kamer", "kamers", "ruimte", "ruimtes", "per", "rooms", "room", "en", "and", "ook", "also", "nou", "dan", "toch",
}
LIGHT_WORDS = {"lamp", "lampen", "licht", "lichten", "verlichting", "lampje", "lampjes", "light", "lights"}
LIGHT_ON_WORDS = {"aan", "brandt", "branden", "on", "uit", "off"}
OPENING_WORDS = {"raam", "ramen", "deur", "deuren", "venster", "vensters", "window", "windows", "door", "doors"}
OPEN_WORDS = {"open", "dicht", "gesloten", "closed", "opened"}
TEMP_WORDS = {"temperatuur", "temperaturen", "warm", "koud", "graden", "temperature", "temperatures", "hot", "cold",
              "vochtig", "luchtvochtigheid", "vocht", "humidity", "co2", "lucht", "klimaat"}
OUTSIDE_WORDS = {"buiten", "outside", "buitentemperatuur"}
PEOPLE_WORDS = {"wie", "who", "iemand", "iedereen", "everyone", "everybody", "anyone", "somebody", "someone", "niemand", "nobody"}
HOME_WORDS = {"thuis", "home", "weg", "away"}


def _areas(index: EntityIndex) -> dict[str, set[str]]:
    return {name: set(normalize(name).split()) for name in index.areas.values()
            if normalize(name) not in NON_ROOM_AREAS}


def _match_areas(index: EntityIndex, tokens: set[str]) -> tuple[list[str], set[str]]:
    """Areas named in the question and the words that named them. "slaapkamer" matches every bedroom when no
    area is named in full."""
    areas = _areas(index)
    full = [name for name, words in areas.items() if words <= tokens]
    if full:
        return sorted(full), set().union(*(areas[n] for n in full))
    loose = [name for name, words in areas.items() if (words & tokens) - FILLER]
    return sorted(loose), set().union(*(areas[n] & tokens for n in loose)) if loose else set()


def _people(index: EntityIndex) -> list[EntityInfo]:
    return sorted((e for e in index.entities.values() if e.domain == "person"), key=lambda e: e.name)


def _person_names(person: EntityInfo) -> set[str]:
    return set(normalize(person.name).split()) | set(person.entity_id.split(".", 1)[1].split("_"))


def _listing(grouped: dict[str, list[str]], voice: bool) -> str:
    if voice or len(grouped) <= 2:
        return "; ".join(f"{area}: {', '.join(names)}" for area, names in grouped.items())
    return "\n".join(f"- {area}: {', '.join(names)}" for area, names in grouped.items())


def _join(listing: str, voice: bool, groups: int) -> str:
    return f" {listing}." if voice or groups <= 2 else "\n" + listing


def _lights(index: EntityIndex, areas: list[str], nl: bool, voice: bool) -> str | None:
    if areas and not any(e.domain == "light" and e.area in areas for e in index.entities.values()):
        return None
    on = [e for e in index.entities.values() if e.domain == "light" and e.state == "on"
          and (not areas or e.area in areas)]
    where = (" in " + " en ".join(areas)) if areas else ""
    if not on:
        return f"Er staan geen lampen aan{where}." if nl else f"No lights are on{where}."
    grouped = index.group_by_area(on)
    if len(grouped) == 1:
        area, names = next(iter(grouped.items()))
        return (f"{'Aan' if nl else 'On'} in {area}: {', '.join(names)}." if area != "zonder ruimte"
                else f"{'Aan' if nl else 'On'}: {', '.join(names)}.")
    count = len(on)
    head = (f"Er {'staat 1 lamp' if count == 1 else f'staan {count} lampen'} aan{where}:" if nl
            else f"{count} light{'s' * (count != 1)} on{where}:")
    return head + _join(_listing(grouped, voice), voice, len(grouped))


def _openings(index: EntityIndex, areas: list[str], nl: bool, voice: bool) -> str | None:
    sensors = [e for e in index.entities.values() if e.state in ("on", "off") and index.is_opening(e)
               and (not areas or e.area in areas)]
    if not sensors:
        return None
    opened = [e for e in sensors if e.state == "on"]
    where = (" in " + " en ".join(areas)) if areas else ""
    if not opened:
        return (f"Alle ramen en deuren met een sensor zijn dicht{where}." if nl
                else f"All doors and windows with a sensor are closed{where}.")
    grouped = index.group_by_area(opened)
    return f"Open{where}:" + _join(_listing(grouped, voice), voice, len(grouped))


def _temperatures(index: EntityIndex, config: Config, profiles: list[dict[str, Any]], areas: list[str],
                  outside: bool, nl: bool, voice: bool) -> str | None:
    out = outside_temperature(index, config)
    out_text = (f"{'Buiten is het' if nl else 'Outside it is'} {fmt_number(out, nl)} °C." if out is not None
                else None)
    if outside and not areas:
        return out_text or ("De buitentemperatuur is niet bekend." if nl else "The outside temperature is unknown.")
    readings = room_readings(index, profiles)
    if areas:
        found = {a: v for a, v in readings.items() if a in areas}
        if not found:
            return None
        return readings_text(found, nl) + "." + (f" {out_text}" if outside and out_text else "")
    readings = {a: v for a, v in readings.items() if normalize(a) not in NON_ROOM_AREAS}
    if not readings:
        return None
    if voice:
        text = readings_text(readings, nl) + "."
    else:
        text = ("Temperatuur per ruimte:\n" if nl else "Temperature per room:\n") + "\n".join(f"- {part}" for part in readings_text(readings, nl).split("; "))
    return text + (("\n" if not voice else " ") + out_text if out_text else "")


def _who(index: EntityIndex, tokens: set[str], nl: bool) -> str | None:
    people = _people(index)
    if not people:
        return None
    named = [p for p in people if (_person_names(p) & tokens) - FILLER - PEOPLE_WORDS - HOME_WORDS]

    def state(p: EntityInfo) -> str:
        if p.state in ("unknown", "unavailable"):
            return "onbekend" if nl else "unknown"
        if p.state in ("home", "not_home"):
            return ("thuis" if nl else "home") if p.state == "home" else ("weg" if nl else "away")
        return f"in {p.state}"  # another zone ("Werk Nikky") is away from home

    if named:
        return " ".join(f"{p.name} is {state(p)}." for p in named)
    home = [p.name for p in people if p.state == "home"]
    others = ", ".join(f"{p.name} is {state(p)}" for p in people if p.state != "home")
    if home:
        head = f"{'Thuis' if nl else 'Home'}: {', '.join(home)}."
        return head + (f" {others[0].upper()}{others[1:]}." if others else "")
    return ("Er is niemand thuis" if nl else "Nobody is home") + (f": {others}." if others else ".")


def status_answer(index: EntityIndex, config: Config, question: str, profiles: list[dict[str, Any]] | tuple = (),
                  voice: bool = False) -> str | None:
    """The answer to a plain status question, or None when the agent should handle it."""
    nl = config.language == "nl"
    car = car_answer(index, question, nl)
    if car:
        return car
    tokens_list = normalize(question).split()
    if not tokens_list or ("?" not in question and tokens_list[0] not in STATUS_START):
        return None
    tokens = set(tokens_list)
    areas, area_words = _match_areas(index, tokens)
    rest = tokens - FILLER - area_words
    if rest <= LIGHT_WORDS | LIGHT_ON_WORDS and rest & LIGHT_WORDS:
        return _lights(index, areas, nl, voice)
    if rest <= OPENING_WORDS | OPEN_WORDS and rest & OPENING_WORDS and (rest & OPEN_WORDS or "welke" in tokens):
        return _openings(index, areas, nl, voice)
    if rest <= TEMP_WORDS | OUTSIDE_WORDS and rest & (TEMP_WORDS | {"buitentemperatuur"}):
        return _temperatures(index, config, list(profiles), areas, bool(rest & OUTSIDE_WORDS), nl, voice)
    people = _people(index)
    names = set().union(*(_person_names(p) for p in people)) if people else set()
    if not areas and tokens & HOME_WORDS and rest <= PEOPLE_WORDS | HOME_WORDS | names \
            and tokens & (PEOPLE_WORDS | names):
        return _who(index, tokens, nl)
    return None
