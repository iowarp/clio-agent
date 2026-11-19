"""A question card's title: who asks, then what they ask.

A server's elicitation is titled with its plugin's display title (or the server name when
it has none) and the first sentence of its message, so a card reads as
``Compute: Submit the job to the cluster?`` instead of a generic ``Question``. The
title is one bounded line; the full message stays the card's prompt.
"""

from __future__ import annotations

import re
from typing import Any

from clio_agent.plugins import host_semantics

GENERIC_TITLE = "Question"
ASK_LIMIT = 90
_SENTENCE_END = re.compile(r"(?<=[.?!:])\s")
_MARKDOWN = re.compile(r"^#{1,6}\s+|\*\*|__|`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _first_sentence(text: str) -> str:
    for line in text.splitlines():
        line = _MARKDOWN.sub("", _LINK.sub(r"\1", line)).strip()
        if line:
            sentence = _SENTENCE_END.split(line, maxsplit=1)[0].strip().rstrip(":").strip()
            if len(sentence) > ASK_LIMIT:
                cut = sentence[: ASK_LIMIT - 1].rsplit(" ", 1)[0].rstrip(" ,;")
                sentence = (cut or sentence[: ASK_LIMIT - 1]) + "\u2026"
            return sentence
    return ""


def asker_name(elicitation: dict[str, Any]) -> str:
    """The display name of the server that asked ("" when the question names none)."""

    server = str(elicitation.get("server") or elicitation.get("namespace") or "")
    title = str(elicitation.get("plugin_title") or "")
    if not title and server:
        plugin = str(elicitation.get("plugin") or "") or host_semantics.server_plugin(server)
        title = str(host_semantics.plugin_host(plugin, server).get("title") or "") if plugin else ""
    return title or server


def question_title(prompt: str, metadata: dict[str, Any]) -> str:
    """The title of a question with ``prompt`` and ``metadata`` (``Question`` when unknown)."""

    elicitation = metadata.get("elicitation")
    name = asker_name(elicitation) if isinstance(elicitation, dict) else ""
    if not name:
        return GENERIC_TITLE
    ask = _first_sentence(prompt or "")
    return f"{name}: {ask}" if ask else name
