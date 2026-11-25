"""A plain reading of a structured MCP result for a tool row's expansion.

What the call found, not how the value is shaped: envelope and integrity fields
(``schema``, ``op``, ``sha256``, ``_*``, cursors) and empty values drop, records read
by their name and version, and long lists are bounded. The exact value stays in the
row's Technical details. The client reads the same value the same way
(gact-tui ``structured-result-reading.ts``); this is the text it shows when the
recorded result is not available to it.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

#: Envelope, bookkeeping and integrity fields: true, but not what the call found.
_TECHNICAL_KEY = re.compile(
    r"^(?:_.*|\$schema|schema|schema_version|ok|op|operation|sha256|digest|checksum|etag"
    r"|cursor|next_cursor|sources)$"
)
_NAME_KEYS = ("name", "title", "label", "id")
_IDENTIFIER = re.compile(r"^[\w.:/@+-]+$")
_IDENTIFIER_MARK = re.compile(r"[._/:@-]|\d")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<])")
_MAX_LINES = 16
_MAX_ITEMS = 8
_MAX_FACTS = 5
_MAX_TEXT = 240

MORE_IN_TECHNICAL_DETAILS = "More values are in Technical details."
NOTHING_TO_SHOW = "The call returned no values to show. Technical details has the exact result."


def _technical(key: Any) -> bool:
    return bool(_TECHNICAL_KEY.match(str(key)))


def _empty(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, list):
        return all(_empty(item) for item in value)
    if isinstance(value, Mapping):
        return all(_technical(key) or _empty(item) for key, item in value.items())
    return False


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _escape(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", html.escape(text, quote=False))


def _literal(text: str) -> str:
    """An identifier reads as code; prose reads as prose."""

    value = " ".join(text.split())
    shown = f"{value[:_MAX_TEXT]}…" if len(value) > _MAX_TEXT else value
    if _IDENTIFIER.match(shown) and _IDENTIFIER_MARK.search(shown) and "`" not in shown:
        return f"`{shown}`"
    return _escape(shown)


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _literal(str(value))


def _heading(key: Any) -> str:
    words = str(key).replace("_", " ").strip()
    return _escape(words[:1].upper() + words[1:])


def _field(key: Any) -> str:
    return _escape(str(key).replace("_", " ").strip())


def _record_name(row: Mapping[str, Any]) -> tuple[str, str] | None:
    for key in _NAME_KEYS:
        value = row.get(key)
        if _scalar(value) and not isinstance(value, bool) and str(value).strip():
            return key, str(value)
    return None


def _scalar_list(values: list[Any]) -> str:
    scalars = [value for value in values if _scalar(value)]
    shown = ", ".join(_scalar_text(value) for value in scalars[:_MAX_ITEMS])
    rest = len(values) - min(len(scalars), _MAX_ITEMS)
    return f"{shown} and {rest} more" if rest > 0 else shown


def _record_phrase(row: Mapping[str, Any], *, nested: bool = True) -> str:
    """``**clio_jarvis.gromacs** `0.1.0`: kind package, size `4096`"``."""

    name = _record_name(row)
    version = row.get("version")
    facts: list[str] = []
    for key, value in row.items():
        if (name and key == name[0]) or key == "version" or (name and key == "id"):
            continue
        if _technical(key) or _empty(value):
            continue
        if _scalar(value):
            facts.append(f"{_field(key)} {_scalar_text(value)}")
        elif isinstance(value, list) and all(_scalar(item) for item in value):
            facts.append(f"{_field(key)} {_scalar_list(value)}")
        elif isinstance(value, Mapping) and nested and (inner := _record_name(value)):
            facts.append(f"{_field(key)} {_literal(inner[1])}")
        if len(facts) >= _MAX_FACTS:
            break
    if not name:
        return ", ".join(facts)
    named = f"**{_escape(name[1])}**" + (
        f" {_literal(str(version))}" if _scalar(version) and not isinstance(version, bool) else ""
    )
    return f"{named}: {', '.join(facts)}" if facts else named


def _lines(row: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in row.items():
        if _technical(key) or _empty(value):
            continue
        if _scalar(value):
            lines.append(f"- {_heading(key)}: {_scalar_text(value)}")
        elif isinstance(value, list):
            items = [item for item in value if not _empty(item)]
            if all(_scalar(item) for item in items):
                lines.append(f"- {_heading(key)}: {_scalar_list(items)}")
                continue
            records = [item for item in items if isinstance(item, Mapping)]
            phrases = [phrase for item in records[:_MAX_ITEMS] if (phrase := _record_phrase(item))]
            if not phrases:
                lines.append(f"- {_heading(key)}: {len(items)} items")
                continue
            lines.append(f"- {_heading(key)} ({len(items)}):")
            lines += [f"  - {phrase}" for phrase in phrases]
            if len(items) > _MAX_ITEMS:
                lines.append(f"  - {len(items) - _MAX_ITEMS} more")
        elif isinstance(value, Mapping) and (phrase := _record_phrase(value)):
            lines.append(f"- {_heading(key)}: {phrase}")
    return lines


def structured_markdown(value: Mapping[str, Any]) -> str:
    """What a structured result found, one plain line per field that says something."""

    lines = _lines(value)
    if not lines:
        return NOTHING_TO_SHOW
    body = "\n".join(lines[:_MAX_LINES])
    return f"{body}\n\n{MORE_IN_TECHNICAL_DETAILS}" if len(lines) > _MAX_LINES else body
