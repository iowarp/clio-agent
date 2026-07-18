"""Delegation + workflow-state derivation helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that turns model-returned *expert handoffs* into executable
delegations and reconciles the *typed workflow_state* that flows back up from
child experts. It pairs with :mod:`clio_agent.gact.turn` (the turn-orchestration
engine imports these), and reuses the pure merge/normalize primitives in
:mod:`clio_agent.gact.workflow_state.merge` rather than duplicating them.

Responsibilities grouped here:

* Expert-handoff coercion + summary (parsing model output into dict rows).
* Child-output compaction that retains exact evidence (paths/identifiers/state).
* Workflow-state derivation: extracting typed state from outputs/handoff rows and
  building the parent-consumable payloads.
* Synchronous delegated-expert prompt assembly + failure state.
* Small typed-state predicates used by reactivity/grounding checks.

The module imports only leaves (stdlib + :mod:`clio_agent.gact.runtime.globals`
for ``_jsonish`` + :mod:`clio_agent.gact.workflow_state.merge` for the merge
primitive); it never imports :mod:`clio_agent.gact.app` at module top. The one
tool-call-evidence helper it needs (``_tool_calls_from_handoff_rows``) still
lives with its sibling tool-call-row helpers in ``app.py`` and is imported
lazily inside the single function that uses it, matching the cycle-break pattern
already used by ``turn.py`` / ``agents/builders.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.globals import _jsonish
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.scientific_suffixes import scientific_suffix_alternation

# Evidence-index suffix vocabulary: the shared scientific vocabulary plus a
# delegation-local "json" extension (compact summaries must retain config /
# manifest paths too). Single source of truth — issue #765.
_EVIDENCE_SUFFIX_PATTERN = scientific_suffix_alternation(extra=("json",))

if TYPE_CHECKING:
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


# ------------------------------------------------------------------------- #
# Expert-handoff coercion + summary #
# ------------------------------------------------------------------------- #


def _coerce_expert_handoff_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize model-returned expert handoff data into dict rows."""

    if value is None:
        return []
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, tuple):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"[]", "null", "None"}:
            return []
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"(\[[\s\S]*\])", text)
            if match is None:
                return []
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                return []
        return _coerce_expert_handoff_rows(parsed)
    return []


def _expert_handoff_fields(handoff: Mapping[str, Any]) -> dict[str, str]:
    """Return the structured handoff fields for an ``expert_handoff`` Part.

    The message Part carries the delegation as typed fields (``parent_agent`` /
    ``child_agent`` / ``stage`` / ``status``) instead of forcing a client to parse
    a prose label. The generating party is the parent, so callers set
    ``Part.agent_id`` to ``parent_agent``.
    """

    child = str(handoff.get("agent_id") or handoff.get("expert") or "").strip()
    parent = str(handoff.get("parent_id") or handoff.get("parent") or "").strip()
    status = str(handoff.get("status") or "observed").strip()
    stage = str(handoff.get("stage") or handoff.get("dispatch_target") or "").strip()
    return {
        "parent_agent": parent,
        "child_agent": child,
        "stage": stage,
        "status": status,
    }


# ------------------------------------------------------------------------- #
# Child-output compaction retaining exact evidence #
# ------------------------------------------------------------------------- #


def _compact_exact_evidence_index(transcript: str) -> str:
    """Build a deterministic evidence index to append to LM compact summaries."""
    paths: list[str] = []
    identifiers: list[str] = []
    caveats: list[str] = []

    def add_unique(target: list[str], value: str, *, limit: int) -> None:
        cleaned = " ".join(value.strip("`'\" \t\r\n,.;:()[]{}").split())
        cleaned = cleaned.rstrip("/")
        if not cleaned or cleaned in target:
            return
        if len(cleaned) > 180:
            cleaned = cleaned[:177] + "..."
        if len(target) < limit:
            target.append(cleaned)

    quoted = re.findall(r"`([^`]+)`", transcript)
    for item in quoted:
        if re.search(rf"\.{_EVIDENCE_SUFFIX_PATTERN}\b", item, re.I):
            add_unique(paths, item, limit=40)
        elif re.search(r"[/_]", item) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,}", item):
            add_unique(identifiers, item, limit=80)

    path_pattern = re.compile(
        rf"(?:[A-Za-z]:\\[^\r\n`\"<>|]*?\.{_EVIDENCE_SUFFIX_PATTERN})"
        rf"|(?:/[^\s`\"<>|]*?\.{_EVIDENCE_SUFFIX_PATTERN})",
        re.I,
    )
    for match in path_pattern.finditer(transcript):
        add_unique(paths, match.group(0), limit=40)

    identifier_pattern = re.compile(
        r"(?<![A-Za-z0-9])/?[A-Za-z][A-Za-z0-9]*(?:[_/.-][A-Za-z0-9]+)+\b",
    )
    for match in identifier_pattern.finditer(transcript):
        value = match.group(0)
        if len(value) < 4:
            continue
        if value.lower().startswith(("http", "https")):
            continue
        add_unique(identifiers, value, limit=80)

    caveat_terms = (
        "error",
        "failed",
        "missing",
        "unavailable",
        "not installed",
        "caveat",
        "unresolved",
        "follow-up",
        "follow up",
        "needs checking",
        "action needed",
    )
    for raw_line in transcript.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        lowered = line.lower()
        if any(term in lowered for term in caveat_terms):
            add_unique(caveats, line, limit=16)

    sections: list[str] = []
    if paths:
        sections.append("Paths:\n" + "\n".join(f"- {path}" for path in paths))
    if identifiers:
        sections.append(
            "Identifiers:\n" + "\n".join(f"- {identifier}" for identifier in identifiers)
        )
    if caveats:
        sections.append("Caveats/errors:\n" + "\n".join(f"- {caveat}" for caveat in caveats))
    if not sections:
        return ""
    return "[exact retained evidence index]\n" + "\n\n".join(sections)


# ------------------------------------------------------------------------- #
# Workflow-state derivation from outputs + handoff rows #
# ------------------------------------------------------------------------- #


def _json_objects_from_text(text: str) -> list[Any]:
    """Extract JSON objects embedded in model/tool evidence without trusting prose."""

    stripped = text.strip()
    objects: list[Any] = []
    decoder = json.JSONDecoder()
    if stripped.startswith(("{", "[")):
        try:
            objects.append(json.loads(stripped))
            return objects
        except json.JSONDecodeError:
            pass
    index = 0
    while index < len(text):
        if text[index] not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        objects.append(value)
        index += max(end, 1)
    return objects


def _merge_workflow_state_from_value(
    value: Any, state: dict[str, Any], *, schema: "WorkflowStateSchema"
) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            for nested in _json_objects_from_text(text):
                _merge_workflow_state_from_value(nested, state, schema=schema)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _merge_workflow_state_from_value(item, state, schema=schema)
        return
    if not isinstance(value, Mapping):
        # A typed workflow_state field may arrive as a Pydantic model when a pack
        # declares it as a nested object signature field. Convert it to a plain
        # mapping so its sections merge. Generic across all packs.
        if callable(getattr(value, "model_dump", None)):
            normalized = _jsonish(value)
            if isinstance(normalized, Mapping):
                _merge_workflow_state_from_value(normalized, state, schema=schema)
        return
    for key in ("workflow_state", "semantic_state", "state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            _merge_workflow_state_mapping(state, nested, schema=schema)
    structured = value.get("structured")
    if isinstance(structured, Mapping):
        for nested in structured.values():
            _merge_workflow_state_from_value(nested, state, schema=schema)
    for key, nested in value.items():
        if key in {"workflow_state", "semantic_state", "state", "structured"}:
            continue
        if isinstance(nested, Mapping):
            if str(key) == "provenance":
                _merge_workflow_state_mapping(state, nested, schema=schema)
            _merge_workflow_state_mapping(state, {str(key): nested}, schema=schema)


def _workflow_state_from_outputs(
    completed_outputs: list[Any], *, schema: "WorkflowStateSchema"
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for output in completed_outputs:
        if isinstance(output, str):
            for obj in _json_objects_from_text(output):
                _merge_workflow_state_from_value(obj, state, schema=schema)
        elif output is not None:
            _merge_workflow_state_from_value(output, state, schema=schema)
    return state


def _workflow_state_payload(state: Mapping[str, Any]) -> str:
    """Return a parseable workflow-state payload for prompts and compact rows."""

    return json.dumps({"workflow_state": state}, sort_keys=True, default=str)


def _workflow_state_from_handoff_rows(
    rows: list[dict[str, Any]], *, schema: "WorkflowStateSchema"
) -> dict[str, Any]:
    """Return durable typed state stored on handoff rows and nested tool rows."""

    state: dict[str, Any] = {}

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        raw_state = row.get("workflow_state")
        if isinstance(raw_state, Mapping):
            _merge_workflow_state_mapping(state, raw_state, schema=schema)
        # #880: typed state rides row["workflow_state"] ONLY. The former
        # summary-prose scrape (parsing typed machine state out of a
        # human-readable summary sentence the server wrote) is deleted — there is
        # no server-authored summary to parse anymore.
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                call_state = call.get("workflow_state")
                if isinstance(call_state, Mapping):
                    _merge_workflow_state_mapping(state, call_state, schema=schema)
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return state


def _prediction_workflow_state(result: Any, *, schema: "WorkflowStateSchema") -> dict[str, Any]:
    """Return a prediction's first-class typed ``workflow_state`` as a Mapping.

    ``workflow_state`` is the ONE load-bearing structured output on the dynamic
    expert signature. This is the STRUCTURED twin of the (now removed) prose
    append: instead of serializing the typed field into the answer text and
    re-parsing it back out (which polluted the user-facing answer), callers read
    the typed field directly via this helper and carry it on the structured
    carrier (the completed/handoff/ledger row's ``workflow_state`` Mapping).

    A typed ``workflow_state`` field may arrive as a Pydantic model (when a pack
    declares it as a nested object signature field), a JSON string, or a plain
    dict. Each is normalized to a plain ``{section: ...}`` mapping. Generic for
    all packs.
    """

    raw_state = getattr(result, "workflow_state", None)
    if raw_state in (None, ""):
        return {}
    if isinstance(raw_state, str):
        text = raw_state.strip()
        if not text:
            return {}
        return _workflow_state_from_outputs([text], schema=schema)
    normalized_state = _jsonish(raw_state)
    if isinstance(normalized_state, Mapping):
        inner = normalized_state.get("workflow_state")
        if isinstance(inner, Mapping):
            return dict(inner)
        return dict(normalized_state)
    return {}
