"""User-facing texts in the configured language (nl or en)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config

TEXTS: dict[str, dict[str, str]] = {
    "summary_title": {"nl": "☀️ Goedemorgen", "en": "☀️ Good morning"},
    "summary_system": {
        "nl": "Je bent de AI-assistent van het huis. Schrijf in het Nederlands.",
        "en": "You are the AI assistant of the house. Write in English.",
    },
    "summary_prompt": {
        "nl": "Schrijf een korte, vriendelijke ochtendsamenvatting (maximaal 6 zinnen, geen opmaak) voor de "
              "bewoners op basis van deze gegevens. Noem het weer, bijzonderheden van de nacht (incidenten), wat je "
              "zelf hebt gedaan, open ramen/sloten en lege batterijen alleen als ze er zijn. Noem afspraken uit de "
              "agenda en, als het iets oplevert, het goedkoopste moment voor stroom of de verwachte zonne-opbrengst.",
        "en": "Write a short, friendly morning summary (at most 6 sentences, no formatting) for the residents "
              "based on this data. Mention the weather, anything notable during the night (incidents), what you "
              "did yourself, and open windows/locks and empty batteries only if there are any. Mention calendar "
              "appointments and, when useful, the cheapest time for electricity or the expected solar yield.",
    },
    "routines_title": {"nl": "💡 Nieuwe routines gevonden", "en": "💡 New routines found"},
    "routines_message": {
        "nl": "Ik heb {count} terugkerende handeling(en) gevonden die ik kan automatiseren. "
              "Bekijk ze in AI Huis → Routines.",
        "en": "I found {count} recurring action(s) that I can automate. See AI Huis → Routines.",
    },
}


def text(config: Config, key: str, **values: Any) -> str:
    variants = TEXTS[key]
    template = variants.get(config.language) or variants["nl"]
    return template.format(**values) if values else template
