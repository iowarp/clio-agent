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
    from clio_agent.gact.types import AgentDef
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


# ------------------------------------------------------------------------- #
# Synchronous delegated-expert prompt assembly #
# ------------------------------------------------------------------------- #


def _delegated_expert_agent_id(row: Mapping[str, Any]) -> str:
    """Return the requested delegated expert id from a handoff row."""

    for key in ("delegate_to", "agent_id", "target_agent_id", "expert"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _delegated_expert_prompt(row: Mapping[str, Any], fallback: str) -> str:
    """Build the child prompt for a synchronous expert delegation."""

    fallback = fallback.strip()
    for key in ("question", "input", "prompt", "request"):
        value = str(row.get(key) or "").strip()
        if value:
            if not fallback or fallback in value:
                return value
            # Pass the FULL parent evidence — clio must not heuristically truncate
            # content a child expert sees; only an LLM may reduce content.
            return "\n\n".join(
                (
                    value,
                    "Parent evidence available for this delegated task:",
                    fallback,
                )
            )
    return fallback


def _delegate_started_row(
    row: Mapping[str, Any],
    *,
    target: "AgentDef",
    parent_id: str,
    depth: int,
    execution_mode: str,
    passed_workflow_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``delegate.started`` handoff row for a sync delegation.

    #888: attach the typed ``workflow_state`` snapshot the parent PASSES INTO the
    child (the same mapping :func:`_append_accumulated_workflow_state_context`
    renders into the child's execution prompt) as a typed carrier on the row — so
    "what was this child seeded with" is visible on the wire, not just composed
    into the prompt. Typed data on a typed carrier: no authored text, no prose.
    Non-empty mapping -> ``workflow_state`` key present; empty/None -> key ABSENT
    (never present-and-empty), matching the #885 shape discipline.
    """

    started: dict[str, Any] = {
        **row,
        "agent_id": target.id,
        "parent_id": parent_id,
        "pack_id": str(target.metadata.get("pack_id") or ""),
        "pack_version": str(target.metadata.get("pack_version") or ""),
        "status": "running",
        "stage": "delegate.started",
        "delegation_lifecycle": "sync",
        "depth": depth,
        "execution_mode": execution_mode,
    }
    if passed_workflow_state:
        started["workflow_state"] = dict(passed_workflow_state)
    return started


# The server's OWN marker constants: the fixed strings that
# ``_delegated_expert_prompt`` / ``_append_accumulated_workflow_state_context``
# APPEND when they compose a child execution prompt by JOINING the public task
# with server-supplied execution context.
# Splitting a SERVER-COMPOSED prompt back at the SERVER's OWN constant recovers
# the public-task half — structural string handling of a string clio authored,
# split at a boundary clio authored — never a heuristic match against arbitrary
# model prose (superseding principle #1). See #881.
_SERVER_APPENDED_CONTEXT_MARKERS: tuple[str, ...] = (
    "Parent evidence available for this delegated task:",
    "Accumulated typed workflow state from prior CLIO tool evidence",
    "Authoritative typed workflow_state accumulated from the completed",
)


def _public_task_from_composed_prompt(prompt: str) -> str:
    """Recover the public task half of a SERVER-composed child prompt.

    :func:`_delegated_expert_prompt` and
    :func:`_append_accumulated_workflow_state_context` build a child prompt by
    joining the public task with server-appended execution context (parent
    evidence, the accumulated ``{"workflow_state": ...}`` block) at the fixed
    marker constants in :data:`_SERVER_APPENDED_CONTEXT_MARKERS`. Because the
    server authored both the join and the markers, splitting the composed string
    back at those owned constants is structural — it recovers the public task
    without ever matching a keyword against model prose.
    """

    public = prompt.strip()
    for marker in _SERVER_APPENDED_CONTEXT_MARKERS:
        index = public.find(marker)
        if index >= 0:
            public = public[:index].rstrip()
    json_index = public.find('{"workflow_state"')
    if json_index >= 0:
        public = public[:json_index].rstrip()
    return public.strip()


def _delegated_expert_public_prompt(row: Mapping[str, Any], fallback: str) -> str:
    """Return the public task text for an agent-call transcript event.

    The parent model's own instruction —
    ``row['question' | 'input' | 'prompt' | 'request']`` — is MODEL OUTPUT and is
    returned VERBATIM. The server no longer scrubs contract vocabulary out of it:
    epic #880's rule is that the client renders verbatim and the server fixes
    leaks at the root, so a sentence in which the model happens to name a typed
    field is the model's own text and stays intact.

    Only the ``fallback`` may be a prompt the SERVER itself composed by appending
    execution context, so it is split back at the server's owned marker constants
    to recover the public task (see :func:`_public_task_from_composed_prompt`).
    """

    for key in ("question", "input", "prompt", "request"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return _public_task_from_composed_prompt(fallback)


def _append_accumulated_workflow_state_context(prompt: str, state: Mapping[str, Any]) -> str:
    """Attach durable typed state to child prompts without relying on prose."""

    if not state:
        return prompt
    block = (
        "Accumulated typed workflow state from prior CLIO tool evidence "
        "(authoritative; use this before local prose summaries):\n"
        f"{json.dumps({'workflow_state': state}, sort_keys=True, default=str)}"
    )
    if block in prompt:
        return prompt
    return "\n\n".join(part for part in (prompt.strip(), block) if part)


def _append_session_workflow_state_context(
    app: Any,
    session_id: str,
    prompt: str,
    *,
    schema: "WorkflowStateSchema",
) -> str:
    """Attach accumulated session tool state to a delegated expert prompt."""

    ledger = getattr(getattr(app, "state", None), "tool_call_ledger", None)
    if not isinstance(ledger, dict):
        return prompt
    rows = ledger.get(session_id)
    if not isinstance(rows, list):
        return prompt
    prior_rows = [row for row in rows if isinstance(row, Mapping)]
    state: dict[str, Any] = {}
    for row in prior_rows:
        row_state = row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state, schema=schema)
    if not state:
        return prompt
    return _append_accumulated_workflow_state_context(prompt, state)


def _should_execute_delegated_handoff(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status in {"skipped", "failed", "cancelled", "completed"}:
        return False
    if row.get("execute") is False:
        return False
    if row.get("execute") is True or row.get("delegate_to") or row.get("target_agent_id"):
        return True
    return status in {"requested", "pending", "delegate", "delegated"}


# ------------------------------------------------------------------------- #
# Failed-child delegation state #
# ------------------------------------------------------------------------- #


def _failed_child_delegation_workflow_state(
    *,
    prompt: str,
    child_agent_id: str,
    parent_agent_id: str,
    error: str,
    message: str,
    tools_called: list[dict[str, Any]],
    schema: "WorkflowStateSchema",
) -> dict[str, Any]:
    """Build typed state for a child failure without discarding prior evidence.

    The generic ``delegation`` bookkeeping section is core's own; the
    domain-specific failure stamps (which sections to mark blocked, the blocker
    prose) are declared by the pack ``schema`` and applied by
    :meth:`WorkflowStateSchema.apply_failure_rules`.
    """

    state = _workflow_state_from_outputs([prompt], schema=schema)
    for tool_row in tools_called:
        row_state = tool_row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state, schema=schema)
    state["delegation"] = {
        "status": "failed",
        "failed_child": child_agent_id,
        "parent": parent_agent_id,
        "error": error,
        "message": message,
    }
    schema.apply_failure_rules(state, child_agent_id=child_agent_id, error=error, message=message)
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


def _fallback_answer_from_delegation(handoffs: list[dict[str, Any]]) -> str:
    """Return the latest compact parent-resume output as answer fallback."""

    for row in reversed(handoffs):
        if str(row.get("stage") or "") != "parent.resumed":
            continue
        if str(row.get("status") or "") not in {"", "completed"}:
            continue
        text = str(row.get("output") or "").strip()
        if text:
            return text
    return ""
