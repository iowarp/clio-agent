"""Project external-input provenance onto the tool-result presentation contract.

The UI half of #1320 re-landed as backend presenter blocks (#1336): the
transcript client renders whatever ``ToolPresentation`` blocks the server
attaches to a ``tool_result`` part, so there is no client-side vocabulary for
external inputs to teach it. This module is the one seam that turns
``tool_provenance_metadata`` output (``gact/artifacts/observer_provenance.py``)
into presenter blocks, appended to whatever a tool's own presenter already
built. It is pure and side-effect-free: callers own emitting/publishing the
result, this module only shapes it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from clio_agent.gact.tool_result_presentation import ToolPresentation

logger = logging.getLogger(__name__)

_DEGRADED_STATUS = "degraded"
_FAILED_STATUS = "failed"
#: Typed reason surfaced on the row when a provenance record cannot be shaped
#: into blocks — the degradation is never silent (cleanup-program ground rule).
_SHAPE_FAILED_DIAGNOSTIC = "provenance_presentation_failed"


def _field(value: Any) -> str:
    """Render one provenance field as block text (format-only, never semantic)."""

    return "" if value is None else str(value)


def with_provenance_blocks(
    presentation: dict[str, Any] | None, provenance: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Append external-input provenance blocks to a tool's presentation.

    Adds one ``link`` block per ``provenance["provenance_inputs"]`` entry
    (block id ``input-source-<i>``), and — when ``provenance_warnings`` is
    non-empty — one ``text`` warning block (id ``provenance-incomplete``)
    naming every unrecognized argument, degrading ``status`` unless it is
    already ``"failed"`` and setting ``diagnostic`` if it was unset.

    This runs on the tool-completion hot path, BEFORE the ``tool.call.completed``
    payload is built (``gact/tool_observer.py``), so a provenance record it
    cannot shape must degrade the ROW, never break the completion: the tool's
    own presentation is returned carrying the typed
    ``provenance_presentation_failed`` diagnostic.

    Args:
        presentation: A tool's own presenter output (a ``ToolPresentation``
            as a dict), or ``None`` when the tool declared no presentation.
        provenance: The output of ``tool_provenance_metadata`` — optionally
            carrying ``provenance_inputs`` and/or ``provenance_warnings``.

    Returns:
        The augmented presentation dict, re-validated through
        ``ToolPresentation`` (``extra="forbid"`` is the schema gate). Returns
        ``presentation`` unchanged (same identity) when there is nothing to
        add, and ``None`` when ``presentation`` is ``None``.
    """
    if presentation is None:
        return None
    inputs = provenance.get("provenance_inputs") or []
    warnings = provenance.get("provenance_warnings") or []
    if not inputs and not warnings:
        return presentation
    try:
        return _augmented(presentation, inputs, warnings)
    except Exception:  # noqa: BLE001 - a provenance record must never break the tool row
        logger.exception(
            "presentation blocks skipped reason=%s provenance=%r",
            _SHAPE_FAILED_DIAGNOSTIC,
            provenance,
        )
        return {
            **presentation,
            "diagnostic": presentation.get("diagnostic") or _SHAPE_FAILED_DIAGNOSTIC,
        }


def _augmented(
    presentation: Mapping[str, Any], inputs: Sequence[Any], warnings: Sequence[Any]
) -> dict[str, Any]:
    """Build the schema-validated presentation carrying the provenance blocks."""

    blocks = [*(presentation.get("blocks") or [])]
    for index, source in enumerate(inputs):
        blocks.append(
            {
                "id": f"input-source-{index}",
                "type": "link",
                "target": "file",
                "label": _field(source.get("name")),
                "uri": _field(source.get("locator")),
                "detail": "external input (not hashed)",
            }
        )
    status = presentation.get("status")
    diagnostic = presentation.get("diagnostic")
    if warnings:
        blocks.append(
            {
                "id": "provenance-incomplete",
                "type": "text",
                "severity": "warning",
                "text": (
                    "Provenance incomplete: an external file argument was not "
                    f"recognized ({_unrecognized(warnings)})"
                ),
            }
        )
        if status != _FAILED_STATUS:
            status = _DEGRADED_STATUS
        if diagnostic is None:
            diagnostic = "provenance_incomplete"

    augmented = {**presentation, "blocks": blocks, "status": status, "diagnostic": diagnostic}
    return ToolPresentation.model_validate(augmented).model_dump(exclude_none=True)


def _unrecognized(warnings: Sequence[Any]) -> str:
    """Name EVERY unrecognized argument — one warning row must not hide the rest."""

    named: list[str] = []
    for warning in warnings:
        name = _field(warning.get("arg")) or _field(warning.get("reason")) or "unrecognized"
        if name not in named:
            named.append(name)
    return ", ".join(named)
