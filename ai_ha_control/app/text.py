"""Markdown from the LLM, turned into plain text for places that cannot render it (push, TTS, voice)."""

from __future__ import annotations

import re

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((?:[^)\s]+)\)")
_RULE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$", re.M)


def plain_text(text: str, speech: bool = False) -> str:
    """Strip markdown markup. Lists keep a "•" (or become sentences when spoken)."""
    if not text:
        return text
    text = _RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\1", text)
    text = _BULLET.sub("" if speech else r"\1• ", text)
    text = text.replace("**", "").replace("__", "")
    if speech:
        # A list read out loud: one sentence per line instead of a run-on.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = " ".join(line if line[-1] in ".!?:;" else line + "." for line in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_emphasis(piece: str) -> str:
    """For streamed pieces, where a whole line is not available: drop bold and code markers only."""
    return piece.replace("**", "").replace("__", "").replace("`", "")
