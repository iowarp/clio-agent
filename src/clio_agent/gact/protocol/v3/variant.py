"""GACT 0.3 frames for a BestOfN / Refine run (Phase 9): the variant tabs.

A run's semantic events (``variant.try``, ``variant.try.delta``, ``variant.selected``,
``variant.closed``)
reach the bus as ``semantic.event`` rows; here they become their own v3 frames, so a
client renders one tab per try without reading semantic payloads:

* ``variant.try.upserted`` (entity ``<variants_id>:<try_index>``) -- a try started,
  ended (its text, score, tokens), or failed;
* ``variant.try.delta`` (same entity) -- the try's live text / thinking;
* ``variant.selected`` (entity ``<variants_id>``) -- the scores or the user's pick and
  comment, and the selected try;
* ``variant.closed`` (entity ``<variants_id>``) -- the run ended without a pick:
  ``status`` ``superseded`` / ``cancelled`` / ``expired`` with its typed ``reason``.

Every other semantic event a try emits keeps its ``semantic.event`` frame and carries
``variants_id`` / ``try_index`` in its payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3 import Projection

__all__ = ["project_variant_event"]

_RUN_KEYS = ("agent_id", "origin", "strategy", "judge", "n", "rubric")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _run_fields(event: Event, outer: Mapping[str, Any], inner: Mapping[str, Any]) -> dict:
    return {
        "variants_id": str(inner.get("variants_id") or ""),
        "session_id": event.session_id,
        **({"run_id": str(outer["turn_id"])} if outer.get("turn_id") else {}),
        **{key: inner[key] for key in _RUN_KEYS if key in inner},
    }


def _try(event: Event, outer: Mapping[str, Any], inner: Mapping[str, Any]) -> Projection:
    index = int(inner.get("try_index") or 0)
    entity = f"{inner.get('variants_id') or ''}:{index}"
    optional = ("text", "score", "tokens", "forked_from", "advice", "error")
    payload = {
        "id": entity,
        **_run_fields(event, outer, inner),
        "try_index": index,
        "scope": str(inner.get("scope") or ""),
        "state": str(inner.get("status") or "running"),
        **{key: inner[key] for key in optional if key in inner},
    }
    return Projection("variant.try.upserted", payload, entity)


def _delta(event: Event, outer: Mapping[str, Any], inner: Mapping[str, Any]) -> Projection:
    del event, outer
    index = int(inner.get("try_index") or 0)
    entity = f"{inner.get('variants_id') or ''}:{index}"
    payload = {
        "id": entity,
        "variants_id": str(inner.get("variants_id") or ""),
        "try_index": index,
        "kind": str(inner.get("kind") or "text"),
        "delta": str(inner.get("delta") or ""),
    }
    return Projection("variant.try.delta", payload, entity)


def _selected(event: Event, outer: Mapping[str, Any], inner: Mapping[str, Any]) -> Projection:
    variants_id = str(inner.get("variants_id") or "")
    payload = {
        **_run_fields(event, outer, inner),
        "selected_index": int(inner.get("selected_index") or 0),
        "selected_scope": str(inner.get("selected_scope") or ""),
        "text": str(inner.get("text") or ""),
        "scores": [
            {"try_index": int(row.get("try_index") or 0), "score": row.get("score")}
            for row in inner.get("scores") or []
            if isinstance(row, Mapping)
        ],
        **{key: inner[key] for key in ("winning_score", "pick", "comment") if key in inner},
    }
    return Projection("variant.selected", payload, variants_id)


def _closed(event: Event, outer: Mapping[str, Any], inner: Mapping[str, Any]) -> Projection:
    variants_id = str(inner.get("variants_id") or "")
    payload = {
        **_run_fields(event, outer, inner),
        "status": str(inner.get("status") or ""),
        "reason": str(inner.get("reason") or ""),
        "question_id": str(inner.get("question_id") or ""),
        "closed_at": str(inner.get("closed_at") or ""),
        "candidates": [
            {
                "try_index": int(row.get("try_index") or 0),
                "scope": str(row.get("scope") or ""),
                "text": str(row.get("text") or ""),
            }
            for row in inner.get("candidates") or []
            if isinstance(row, Mapping)
        ],
        **(
            {"superseded_by_message_id": str(inner["superseded_by_message_id"])}
            if inner.get("superseded_by_message_id")
            else {}
        ),
    }
    return Projection("variant.closed", payload, variants_id)


_PROJECTORS = {
    "variant.try": _try,
    "variant.try.delta": _delta,
    "variant.selected": _selected,
    "variant.closed": _closed,
}


def project_variant_event(event: Event, payload: dict[str, Any], session: Any) -> Projection | None:
    """A ``semantic.event`` row of a variant run as its v3 frame (``None``: any other)."""
    del session
    projector = _PROJECTORS.get(str(payload.get("event_type") or ""))
    if projector is None:
        return None
    return projector(event, payload, _mapping(payload.get("payload")))
