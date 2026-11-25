"""A question card's title (what is asked) and its asker (who asks).

A server's elicitation is titled with the first sentence of its message, so the bell,
notifications and cards read ``Submit the job to the cluster?`` instead of a generic
``Question``. Who asks stays a separate field: its plugin's display title, or the server
name when it has none, for attribution beside the title. The title is one bounded line;
the full message stays the card's prompt.
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


def question_asker(metadata: dict[str, Any]) -> str:
    """Who asked a question with ``metadata`` ("" when no server asked it)."""

    elicitation = metadata.get("elicitation")
    return asker_name(elicitation) if isinstance(elicitation, dict) else ""


def question_title(prompt: str, metadata: dict[str, Any]) -> str:
    """The title of a question with ``prompt`` and ``metadata`` (``Question`` when unknown).

    The title is the question alone; :func:`question_asker` names who asked it.
    """

    if not question_asker(metadata):
        return GENERIC_TITLE
    return _first_sentence(prompt or "") or GENERIC_TITLE
