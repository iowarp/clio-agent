"""Deterministic, bounded narration for A2UI action/error records (S5, S5b).

Replaces ``routes/a2ui.py``'s deleted ``_agent_submit_context``/
``_agent_submit_model_text`` (docs/design/a2ui-compat-campaign-2026-09.md S5
deletion inventory): those two helpers only ever ran for the single
``agent.submit`` name and required a ``text``/``prompt`` field. Every A2UI
event now reaches the agent lane this way, with NO name and NO required
field — the resolved ``context`` object is the authoritative input regardless
of what (if anything) it contains.

S5b (clio-agent#1363 live-gate finding): a resumed turn read the legacy
``A2UI event: <name>`` + JSON form as a REPORT, not a REQUEST, and never
acted on it. The event's MEANING is the pack author's to declare —
``CatalogSidecar.events[<name>].narration`` (clio-schemas 0.3.2) — so
:func:`narration_for` now renders that declared template
(``clio_schemas.a2ui.sidecar.render_narration``) when the sidecar route
carries one, falling back to the legacy name+context form (unchanged byte
for byte) only when it does not. Either way the canonical context JSON
travels in a second paragraph, so a text-only provider still receives the
full structured object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clio_schemas.a2ui.sidecar import render_narration

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


def _encoded_context(context: Mapping[str, Any]) -> str:
    return json.dumps(dict(context), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _legacy_narration_for(action_name: str, context: Mapping[str, Any]) -> str:
    """The pre-S5b name+context form, unchanged (the undeclared fallback)."""

    user_message = str(context.get(_USER_MESSAGE_CONTEXT_KEY) or "").strip()
    rest = {key: value for key, value in context.items() if key != _USER_MESSAGE_CONTEXT_KEY}
    header = f"A2UI event: {action_name}" if action_name else "A2UI event"
    sections = [section for section in (user_message, header) if section]
    if rest:
        sections.append(f"Structured context:\n{_encoded_context(rest)}")
    return _bounded("\n\n".join(sections) if sections else header)


def narration_declared(route: Any) -> bool:
    """True iff ``route`` (a sidecar ``events[<name>]`` entry) declares a narration.

    Args:
        route: The action's resolved sidecar route (``action["route"]`` from
            ``gact/a2ui.py::validate_client_action``), or ``None`` when the
            sidecar names no route for this event at all.
    """

    return route is not None and getattr(route, "narration", None) is not None


def narration_for(action_name: str, context: Mapping[str, Any], *, route: Any = None) -> str:
    """Compose the bounded, deterministic text delivered for one A2UI event.

    Args:
        action_name: The event's ``name`` (e.g. ``"agent.submit"``,
            ``"earthscope.stations.selected"``).
        context: The action's resolved ``context`` object, verbatim.
        route: The action's resolved sidecar route (S5b), or ``None``. When
            it declares a ``narration`` template, that template — rendered
            against ``context`` via
            ``clio_schemas.a2ui.sidecar.render_narration`` — IS the pack
            author's declared meaning of this event and leads the text.

    Returns:
        When ``route`` declares a ``narration`` template: the rendered
        template (bounded to :data:`MAX_NARRATION_BYTES`, with the existing
        truncation marker), followed by the canonical context JSON on a
        second, UNBOUNDED paragraph — the structured object always travels
        in text, even for a text-only provider, regardless of how long the
        rendered narration is.

        Otherwise: the legacy ``context.userMessage`` (if any) + event name +
        canonical context JSON form, bounded as a whole (unchanged from S5).
    """

    if not narration_declared(route):
        return _legacy_narration_for(action_name, context)
    rendered = render_narration(route, context) or ""
    return f"{_bounded(rendered)}\n\nStructured context:\n{_encoded_context(context)}"


def repair_narration(surface_id: str, path: str, message: str) -> str:
    """Compose the ONE repair-delivery narration for a ``VALIDATION_FAILED`` report."""

    text = (
        f"renderer rejected surface {surface_id} at {path}: {message}; repair with "
        "update_a2ui_components(...) or load_skill(...)"
    )
    return _bounded(text)


__all__ = ["MAX_NARRATION_BYTES", "narration_declared", "narration_for", "repair_narration"]
