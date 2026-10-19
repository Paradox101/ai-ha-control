"""Ask the vision model what a camera sees, with a category so we only bother the user when it matters."""

from __future__ import annotations

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
    "NIETS - niemand en niets bijzonders (ook alleen licht, schaduw, regen, wind of insecten)\n"
    "VOORBIJGANGER - mensen of voertuigen op straat of de stoep die voorbijgaan, niet op het eigen terrein\n"
    "BEZOEK - iemand of een voertuig op het eigen terrein of bij de deur (bezorger, bezoek, bewoner)\n"
    "VERDACHT - iets ongewoons of zorgwekkends op het eigen terrein\n"
    "Beschrijf daarna in maximaal twee zinnen in het Nederlands wat je ziet."
)


def parse(text: str) -> tuple[str, str]:
    """Split 'BEZOEK: een bezorger bij de deur' into (category, description)."""
    text = text.strip()
    first, _, rest = text.partition(" ")
    category = CATEGORIES.get(first.strip(" :-–.*").upper())
    if category is None:
        return UNKNOWN, text
    return category, rest.strip(" :-–.*") or text


async def classify(llm: OllamaClient, image: bytes, camera: str, context: str = "",
                   priority: int = PRIORITY_SAFETY) -> tuple[str, str]:
    reply = await llm.describe_image(image, PROMPT.format(camera=camera, context=context), priority=priority)
    return parse(reply)
