"""Ask the vision model what a camera sees, with a category so we only bother the user when it matters."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from ..llm.ollama import PRIORITY_SAFETY

if TYPE_CHECKING:
    from ..llm.ollama import OllamaClient

NOTHING, PASSERBY, VISITOR, SUSPICIOUS, UNKNOWN = "nothing", "passerby", "visitor", "suspicious", "unknown"
CATEGORIES = {"NIETS": NOTHING, "VOORBIJGANGER": PASSERBY, "BEZOEK": VISITOR, "VERDACHT": SUSPICIOUS}
QUIET_CATEGORIES = {NOTHING, PASSERBY}  # logged, never pushed

PROMPT = (
    "Dit is het actuele beeld van camera '{camera}'. {context}\n"
    "Kies eerst precies één categorie en schrijf die als eerste woord:\n"
    "NIETS - niemand en niets bijzonders (ook geparkeerde of stilstaande auto's, licht, schaduw, regen, "
    "wind of insecten)\n"
    "VOORBIJGANGER - mensen of voertuigen op straat of de stoep die voorbijgaan, niet op het eigen terrein\n"
    "BEZOEK - iemand of een voertuig op het eigen terrein of bij de deur (bezorger, bezoek, bewoner)\n"
    "VERDACHT - iets ongewoons of zorgwekkends op het eigen terrein\n"
    "Zeg daarna in één korte zin in het Nederlands wie of wat er gebeurt, bijvoorbeeld "
    "'Persoon op fiets rijdt voorbij' of 'Bezorger met pakket bij de deur'. "
    "Beschrijf de omgeving (straat, huizen, auto's, beplanting, weer) niet."
)
# Ollama structured output: the model must answer with exactly these fields (more reliable than parsing text).
SCHEMA = {
    "type": "object",
    "properties": {
        "categorie": {"type": "string", "enum": list(CATEGORIES)},
        "beschrijving": {"type": "string"},
    },
    "required": ["categorie", "beschrijving"],
}
JSON_HINT = "\nAntwoord als JSON met de velden 'categorie' en 'beschrijving'."

# Sentences about the surroundings, which the user doesn't need in a notification.
_SCENERY = re.compile(r"^(de|het|op de achtergrond|in de achtergrond|verder)\b.*\b(omgeving|achtergrond|woonwijk|straatbeeld)\b",
                      re.IGNORECASE)


def _short(text: str) -> str:
    """First sentence, minus scenery: 'Een persoon fietst voorbij. De omgeving toont...' -> 'Een persoon fietst voorbij.'"""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]
    kept = [s for s in sentences if not _SCENERY.search(s)]
    return (kept or sentences or [""])[0]


def parse(text: str) -> tuple[str, str]:
    """Read {"categorie": ..., "beschrijving": ...}, or split 'BEZOEK: een bezorger bij de deur'."""
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            category = CATEGORIES.get(str(data.get("categorie", "")).strip().upper())
            description = _short(str(data.get("beschrijving") or ""))
            return (category or UNKNOWN), description or text
    match = re.match(r"[\W_]*([A-Za-z]+)[\W_]*", text)  # also '**VOORBIJGANGER**\nEen persoon ...'
    category = CATEGORIES.get(match.group(1).upper()) if match else None
    if category is None:
        return UNKNOWN, _short(text)
    return category, _short(text[match.end():]) or text


async def classify(llm: OllamaClient, image: bytes, camera: str, context: str = "",
                   priority: int = PRIORITY_SAFETY) -> tuple[str, str]:
    reply = await llm.describe_image(image, PROMPT.format(camera=camera, context=context) + JSON_HINT,
                                     priority=priority, schema=SCHEMA)
    return parse(reply)
