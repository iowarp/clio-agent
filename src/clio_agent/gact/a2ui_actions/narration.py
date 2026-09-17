"""Deterministic, bounded narration for A2UI action/error records (S5).

Replaces ``routes/a2ui.py``'s deleted ``_agent_submit_context``/
``_agent_submit_model_text`` (docs/design/a2ui-compat-campaign-2026-09.md S5
deletion inventory): those two helpers only ever ran for the single
``agent.submit`` name and required a ``text``/``prompt`` field. Every A2UI
event now reaches the agent lane this way, with NO name and NO required
field — the resolved ``context`` object is the authoritative input regardless
of what (if anything) it contains.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: Hard byte ceiling for one composed narration string.
MAX_NARRATION_BYTES = 2048

#: Reserved 1.0 field (owner decision, docs/gact/a2ui-binding.md): tolerated
#: today, never required. When present it leads the narration verbatim.
_USER_MESSAGE_CONTEXT_KEY = "userMessage"


def _bounded(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_NARRATION_BYTES:
        return text
    return encoded[:MAX_NARRATION_BYTES].decode("utf-8", errors="ignore") + "…"


def narration_for(action_name: str, context: Mapping[str, Any]) -> str:
    """Compose the bounded, deterministic user-steer text for one A2UI event.

    Args:
        action_name: The event's ``name`` (e.g. ``"agent.submit"``,
            ``"earthscope.stations.selected"``).
        context: The action's resolved ``context`` object, verbatim.

    Returns:
        Text prefixed by ``context.userMessage`` when present, followed by
        the event name and the canonical JSON of the rest of the context —
        bounded to :data:`MAX_NARRATION_BYTES`.
    """

    user_message = str(context.get(_USER_MESSAGE_CONTEXT_KEY) or "").strip()
    rest = {key: value for key, value in context.items() if key != _USER_MESSAGE_CONTEXT_KEY}
    header = f"A2UI event: {action_name}" if action_name else "A2UI event"
    sections = [section for section in (user_message, header) if section]
    if rest:
        encoded = json.dumps(rest, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        sections.append(f"Structured context:\n{encoded}")
    return _bounded("\n\n".join(sections) if sections else header)


def repair_narration(surface_id: str, path: str, message: str) -> str:
    """Compose the ONE repair-delivery narration for a ``VALIDATION_FAILED`` report."""

    text = (
        f"renderer rejected surface {surface_id} at {path}: {message}; repair with "
        "update_a2ui_components(...) or load_skill(...)"
    )
    return _bounded(text)


__all__ = ["MAX_NARRATION_BYTES", "narration_for", "repair_narration"]
