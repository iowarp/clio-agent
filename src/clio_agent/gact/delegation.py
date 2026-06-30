"""Delegation + workflow-state derivation helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that turns model-returned *expert handoffs* into executable
delegations and reconciles the *typed workflow_state* that flows back up from
child experts. It pairs with :mod:`clio_agent.gact.turn` (the turn-orchestration
engine imports these), and reuses the pure merge/normalize primitives in
:mod:`clio_agent.gact.workflow_state.merge` rather than duplicating them.

Responsibilities grouped here:

* Expert-handoff coercion + summary (parsing model output into dict rows).
* Delegation continuation: the parent resume prompt + nested return-row walks.
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
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.globals import _jsonish
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


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

    Mirrors the keys :func:`_expert_handoff_summary` reads so the message Part can
    carry the delegation as typed fields (``parent_agent`` / ``child_agent`` /
    ``stage`` / ``status``) instead of forcing a client to parse the prose label.
    The generating party is the parent, so callers set ``Part.agent_id`` to
    ``parent_agent``.
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


def _expert_handoff_summary(handoff: Mapping[str, Any]) -> str:
    """Return a compact user-facing summary for an expert handoff part."""

    agent = str(handoff.get("agent_id") or handoff.get("expert") or "expert")
    parent = str(handoff.get("parent_id") or handoff.get("parent") or "").strip()
    status = str(handoff.get("status") or "observed")
    stage = str(handoff.get("stage") or handoff.get("dispatch_target") or "").strip()
    output = str(
        handoff.get("output") or handoff.get("output_summary") or handoff.get("summary") or ""
    ).strip()
    route = f"{parent} -> {agent}" if parent else agent
    bits = [route, status]
    if stage:
        bits.append(stage)
    if output:
        bits.append(output)
    return " | ".join(bits)


# ------------------------------------------------------------------------- #
# Delegation continuation: parent resume + nested return-row walks #
# ------------------------------------------------------------------------- #


def _dynamic_parent_resume_prompt(
    original_request: str,
    parent_agent: "AgentDef",
    executed_handoffs: list[dict[str, Any]],
    declared_child_ids: set[str] | None = None,
) -> str:
    """Build the compact continuation prompt given back to a dynamic parent."""

    rows: list[str] = []
    merged_state: dict[str, Any] = {}
    completed_ids: list[str] = []
    for row in executed_handoffs:
        if str(row.get("stage") or "") != "delegate.completed":
            continue
        agent_id = str(row.get("agent_id") or row.get("delegate_to") or "")
        if agent_id and agent_id not in completed_ids:
            completed_ids.append(agent_id)
        status = str(row.get("status") or "")
        summary = str(
            row.get("output") or row.get("output_summary") or row.get("summary") or ""
        ).strip()
        children = row.get("children")
        child_note = ""
        if isinstance(children, list) and children:
            child_note = f"; nested_child_events={len(children)}"
        rows.append(f"- {agent_id}: status={status}{child_note}; result={summary}")
        child_state = row.get("workflow_state")
        if isinstance(child_state, Mapping):
            _merge_workflow_state_mapping(merged_state, child_state)
    result_block = "\n".join(rows) or "- No completed child delegation results were returned."
    # Surface the MERGED typed workflow_state from the completed children, not just the
    # prose summaries. A child may put its key result ONLY in the typed field (e.g.
    # qwopus writes acquisition.metadata_path / station_catalog.station_ids into
    # workflow_state but not into its prose answer); without this the parent cannot see
    # the child already delivered, and re-delegates to it in a loop.
    state_block = ""
    if merged_state:
        state_block = (
            "\n\nAuthoritative typed workflow_state accumulated from the completed "
            "children — read these typed fields (e.g. acquisition.metadata_path, "
            "station_catalog.station_ids, acquisition.status, profile.status) to decide "
            "the next step. A child whose result already appears here is DONE; do NOT "
            "re-delegate to it:\n" + _workflow_state_payload(merged_state)
        )
    # Show the orchestrator its own progress as a visible to-do list, so it does not
    # have to track "which of my children have run" mentally across re-invocations
    # (small models lose that thread and finish early). This is reactive grounding
    # (showing state), not forced routing — the agent still decides the next hop, and
    # a child being "not yet run" is informational, not an order to run it.
    progress_block = ""
    if declared_child_ids:
        remaining = [c for c in sorted(declared_child_ids) if c not in completed_ids]
        progress_block = (
            "\n\nYour delegation progress this turn — "
            f"your child experts: {sorted(declared_child_ids)}; "
            f"already run: {completed_ids or '[]'}; "
            f"not yet run: {remaining or '[]'}. "
            "You are the orchestrator: keep delegating to the children this task still "
            "needs, and finish only when the work is genuinely complete. Not every child "
            "is needed for every request — use judgment: skip the ones the evidence makes "
            "unnecessary (e.g. analysis/visualization when there is no data staged), but "
            "do not finish prematurely while a needed step has not run."
        )
    return (
        f"Original user request:\n{original_request}\n\n"
        f"Returned child expert results for parent expert {parent_agent.id!r}:\n"
        f"{result_block}{state_block}{progress_block}\n\n"
        "Continue from these results. Decide the next step via your next_expert / "
        "next_task output: route to the next child the task still needs, or set "
        "next_expert='finish' and write the final answer when the work is genuinely "
        "complete. You MAY go back and re-invoke a child you already ran when you need "
        "MORE or DIFFERENT results from it (e.g. more candidates, a wider search, the "
        "next-ranked item, a retry with corrected arguments) — give it a NEW, specific "
        "sub-task that says what additional result you need. Only restriction: do NOT "
        "re-delegate to repeat work that is ALREADY captured in the typed workflow_state "
        "above (same task, same result already present) — that is a loop, not progress."
    )


def _iter_delegation_return_rows(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield completed delegation rows, including nested child return rows."""

    for row in rows:
        if row.get("stage") == "delegate.completed":
            yield row
        children = row.get("children")
        if isinstance(children, list):
            child_rows = [child for child in children if isinstance(child, dict)]
            yield from _iter_delegation_return_rows(child_rows)


def _latest_parent_resumed_output_summary(
    rows: list[dict[str, Any]],
    parent_id: str,
) -> str:
    """Return the latest compact output from a resumed delegated parent."""

    latest = ""
    stack = list(rows)
    while stack:
        row = stack.pop(0)
        if (
            str(row.get("agent_id") or "") == parent_id
            and str(row.get("stage") or "") == "parent.resumed"
        ):
            summary = str(
                row.get("output") or row.get("output_summary") or row.get("summary") or ""
            ).strip()
            if summary:
                latest = summary
        children = row.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return latest


def _latest_delegation_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return the latest completed delegated child output from nested rows."""

    latest = ""
    for row in _iter_delegation_return_rows(rows):
        summary = str(
            row.get("output") or row.get("output_summary") or row.get("summary") or ""
        ).strip()
        if summary:
            latest = summary
    return latest


def _latest_completed_artifact_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return the latest completed child output that contains final artifact evidence."""

    latest = ""
    stack = list(rows)
    while stack:
        row = stack.pop(0)
        if str(row.get("stage") or "") == "delegate.completed" and str(row.get("status") or "") in {
            "",
            "completed",
        }:
            summary = str(
                row.get("output") or row.get("output_summary") or row.get("summary") or ""
            ).strip()
            if re.search(r"(?im)^\s*(?:FINAL_ARTIFACT|ARTIFACT)\s*:", summary):
                latest = summary
        children = row.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return latest


def _latest_completed_child_output_summary(
    rows: list[dict[str, Any]],
    child_ids: Iterable[str],
) -> str:
    """Return the latest completed output from one of the named child experts."""

    target_ids = {str(child_id).strip() for child_id in child_ids if str(child_id).strip()}
    if not target_ids:
        return ""
    latest = ""
    for row in _iter_delegation_return_rows(rows):
        if (
            str(row.get("stage") or "") == "delegate.completed"
            and str(row.get("status") or "") in {"", "completed"}
            and str(row.get("agent_id") or row.get("delegate_to") or "").strip() in target_ids
        ):
            summary = str(
                row.get("output") or row.get("output_summary") or row.get("summary") or ""
            ).strip()
            if summary:
                latest = summary
    return latest


def _latest_final_child_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return completed synthesis/final-report output when a parent finalizes poorly."""

    return _latest_completed_child_output_summary(
        rows,
        ("synthesis", "final", "final_report", "report", "summary"),
    )


def _bubbled_child_evidence_output_summary(
    rows: list[dict[str, Any]],
    parent_id: str,
    declared_child_ids: Iterable[str],
) -> str:
    """Return the best child-subtree result for strict-depth parent completion."""

    return _latest_parent_resumed_output_summary(
        rows,
        parent_id,
    ) or _latest_completed_child_output_summary(rows, declared_child_ids)


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
        if re.search(r"\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar)\b", item, re.I):
            add_unique(paths, item, limit=40)
        elif re.search(r"[/_]", item) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,}", item):
            add_unique(identifiers, item, limit=80)

    path_pattern = re.compile(
        r"(?:[A-Za-z]:\\[^\r\n`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))"
        r"|(?:/[^\s`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))",
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


def _merge_workflow_state_from_value(value: Any, state: dict[str, Any]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            for nested in _json_objects_from_text(text):
                _merge_workflow_state_from_value(nested, state)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _merge_workflow_state_from_value(item, state)
        return
    if not isinstance(value, Mapping):
        # A typed workflow_state field may arrive as a Pydantic model when a pack
        # declares it as a nested object signature field. Convert it to a plain
        # mapping so its sections merge. Generic across all packs.
        if callable(getattr(value, "model_dump", None)):
            normalized = _jsonish(value)
            if isinstance(normalized, Mapping):
                _merge_workflow_state_from_value(normalized, state)
        return
    for key in ("workflow_state", "semantic_state", "state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            _merge_workflow_state_mapping(state, nested)
    structured = value.get("structured")
    if isinstance(structured, Mapping):
        for nested in structured.values():
            _merge_workflow_state_from_value(nested, state)
    for key, nested in value.items():
        if key in {"workflow_state", "semantic_state", "state", "structured"}:
            continue
        if isinstance(nested, Mapping):
            if str(key) == "provenance":
                _merge_workflow_state_mapping(state, nested)
            _merge_workflow_state_mapping(state, {str(key): nested})


def _workflow_state_from_outputs(completed_outputs: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for output in completed_outputs:
        if isinstance(output, str):
            for obj in _json_objects_from_text(output):
                _merge_workflow_state_from_value(obj, state)
        elif output is not None:
            _merge_workflow_state_from_value(output, state)
    return state


def _workflow_state_payload(state: Mapping[str, Any]) -> str:
    """Return a parseable workflow-state payload for prompts and compact rows."""

    return json.dumps({"workflow_state": state}, sort_keys=True, default=str)


def _workflow_state_from_handoff_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return durable typed state stored on handoff rows and nested tool rows."""

    state: dict[str, Any] = {}

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        raw_state = row.get("workflow_state")
        if isinstance(raw_state, Mapping):
            _merge_workflow_state_mapping(state, raw_state)
        for output_key in ("output_summary", "summary"):
            output = str(row.get(output_key) or "").strip()
            if output:
                _merge_workflow_state_mapping(state, _workflow_state_from_outputs([output]))
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                call_state = call.get("workflow_state")
                if isinstance(call_state, Mapping):
                    _merge_workflow_state_mapping(state, call_state)
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


def _delegated_expert_public_prompt(row: Mapping[str, Any], fallback: str) -> str:
    """Return the public task text for an agent-call transcript event.

    This is intentionally narrower than :func:`_delegated_expert_prompt`: child
    execution may need parent evidence and typed workflow state, but the GACT
    transcript action prompt is the task shown under ``call(agent)``. Execution
    context must travel privately in the child prompt or structurally in state
    events, not as public transcript prose.
    """

    for key in ("question", "input", "prompt", "request"):
        value = str(row.get(key) or "").strip()
        if value:
            return _clean_public_delegation_prompt(value)
    return _clean_public_delegation_prompt(fallback)


def _clean_public_delegation_prompt(text: str) -> str:
    """Strip CLIO execution contract context from a public agent-call prompt."""

    public = text.strip()
    for marker in (
        "Parent evidence available for this delegated task:",
        "Accumulated typed workflow state from prior CLIO tool evidence",
        "Authoritative typed workflow_state accumulated from the completed",
    ):
        index = public.find(marker)
        if index >= 0:
            public = public[:index].rstrip()

    json_index = public.find('{"workflow_state"')
    if json_index >= 0:
        public = public[:json_index].rstrip()

    state_path = (
        r"(?:acquisition|analysis|artifacts|datasets?|evidence|geospatial|region|station_catalog)"
        r"\.[A-Za-z0-9_]+"
    )
    state_field = r"(?:metadata_path|analysis_ready|workflow[_ ]state|structured state)"
    public = re.sub(
        rf"(?is)\s*\(\d+\)\s*[^.;\n]*\b(?:{state_path}|{state_field})\b[^.;\n]*(?:[.;]|$)",
        " ",
        public,
    )
    public = re.sub(
        rf"(?is)\s+using\b[^.?!\n]*\b(?:{state_path}|{state_field})\b[^.?!\n]*(?=[.?!]|$)",
        "",
        public,
    )
    public = re.sub(
        rf"(?is)(^|[.!?]\s+)Until\b[^.\n]*?\b{state_path}\b[^.\n]*?\bworkflow\s+state\b,\s*[^.?!\n]*(?:[.?!]|$)",
        lambda match: match.group(1) if match.group(1).strip() else "",
        public,
    )
    public = re.sub(
        rf"(?is)\s*,?\s*\b(?:which|that)\s+[^.?!\n]*\b{state_path}\b[^.?!\n]*(?:\bworkflow[_ ]state\b[^.?!\n]*)?",
        "",
        public,
    )

    # Public call prompts should describe the work, not the private CLIO output
    # carrier. Keep the rest of the task while removing the contract sentence.
    public = re.sub(
        r"(?is)(^|[.!?]\s+)[^.?!\n]*\bworkflow_state\b[^.?!\n]*(?:[.?!]|$)",
        lambda match: match.group(1) if match.group(1).strip() else "",
        public,
    )
    state_field_sentence_pattern = re.compile(
        rf"(?is)(^|[.!?]\s+)[^.?!\n]*\b{state_path}\b[^.?!\n]*(?:[.?!]|$)"
    )
    while True:
        cleaned = state_field_sentence_pattern.sub(
            lambda match: match.group(1) if match.group(1).strip() else "",
            public,
        )
        if cleaned == public:
            break
        public = cleaned
    public = re.sub(
        rf"(?is)\s*\([^()\n]*\b{state_path}\b[^()\n]*\)",
        "",
        public,
    )
    public = re.sub(
        r"(?is)(^|[.!?]\s+)[^.?!\n]*\bworkflow\s+state\b[^.?!\n]*(?:[.?!]|$)",
        lambda match: match.group(1) if match.group(1).strip() else "",
        public,
    )
    public = re.sub(
        r"(?is)([.!?])\s+\b(?:acquisition|analysis|artifacts|datasets?|evidence|region)\s+so\s+that\b[^.?!\n]*(?:[.?!]|$)",
        r"\1",
        public,
    )
    public = re.sub(
        r"(?is)([.!?])\s+\b(?:acquisition|analysis|artifacts|datasets?|evidence|region)\s*[.?!]\s+",
        r"\1 ",
        public,
    )
    public = re.sub(
        r"(?is)([.!?])\s+\b(?:acquisition|analysis|artifacts|datasets?|evidence|region)\s*[.?!]\s*$",
        r"\1",
        public,
    )
    return re.sub(r"[ \t]+\n", "\n", public).strip()


def _clean_public_transcript_text(text: str) -> str:
    """Strip CLIO contract prose from visible thought/answer transcript text."""

    public = _clean_public_delegation_prompt(text)
    public = re.sub(
        r"(?ims)\n*\s*[A-Za-z ]*region:\s*```(?:json)?\s*[\s\S]*?\bworkflow_state\b[\s\S]*?```",
        "",
        public,
    )
    public = re.sub(
        r"(?ims)```(?:json)?\s*[\s\S]*?\bworkflow_state\b[\s\S]*?```",
        "",
        public,
    )
    public = re.sub(
        r"(?ims)(^|\n\n)[^\n]*(?:\bmetadata_path\b|\banalysis_ready\b|\bstructured state\b)[\s\S]*?(?=\n\n|$)",
        lambda match: match.group(1) if match.group(1).strip() else "",
        public,
    )
    public = re.sub(
        r"(?is)(^|[.!?]\s+)[^.?!\n]*\btyped\s+workflow[_ ]state\b[^.?!\n]*(?:[.?!]|$)",
        lambda match: match.group(1) if match.group(1).strip() else "",
        public,
    )
    return re.sub(r" {2,}", " ", public).strip()


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
            _merge_workflow_state_mapping(state, row_state)
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
) -> dict[str, Any]:
    """Build typed state for a child failure without discarding prior evidence."""

    state = _workflow_state_from_outputs([prompt])
    for tool_row in tools_called:
        row_state = tool_row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state)
    state["delegation"] = {
        "status": "failed",
        "failed_child": child_agent_id,
        "parent": parent_agent_id,
        "error": error,
        "message": message,
    }
    acquisition = state.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("analysis_ready") is not True:
        acquisition["status"] = "blocked"
        acquisition["analysis_ready"] = False
        acquisition["blocker"] = (
            f"child expert {child_agent_id!r} failed before completing acquisition: {error}"
        )
    resource_discovery = state.get("resource_discovery")
    if isinstance(resource_discovery, dict):
        resource_discovery["status"] = "child_failed"
        resource_discovery["blocker"] = (
            f"child expert {child_agent_id!r} failed before completing resource discovery"
        )
        resource_discovery["next_action"] = (
            "retry the child expert after provider availability is restored"
        )
    return state


def _failed_child_delegation_output_summary(
    *,
    child_agent_id: str,
    parent_agent_id: str,
    error: str,
    message: str,
) -> str:
    """Return compact parent-consumable text for a failed child expert.

    The failure's typed ``workflow_state`` is carried STRUCTURALLY on the failed
    delegation row's ``workflow_state`` field (see the caller); it is NOT
    serialized into this human-readable summary text anymore.
    """

    return (
        f"Child expert {child_agent_id!r} failed while delegated from "
        f"{parent_agent_id!r}: {error}. {message}"
    )


# ------------------------------------------------------------------------- #
# Typed workflow-state predicates (reactivity / grounding) #
# ------------------------------------------------------------------------- #


def _state_path_value(state: Mapping[str, Any], path: str) -> Any:
    current: Any = state
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _workflow_state_has_existing_staged_path(state: Mapping[str, Any]) -> bool:
    acquisition = state.get("acquisition")
    if not isinstance(acquisition, Mapping):
        return True
    status = str(acquisition.get("status") or "").strip().lower()
    if status != "staged" or acquisition.get("analysis_ready") is not True:
        return True
    local_path = str(acquisition.get("local_path") or acquisition.get("path") or "").strip()
    if not local_path.startswith(("/", "~")):
        return True
    return Path(local_path).expanduser().is_file()


def _state_predicate_hit(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        if "exists" in expected:
            return (actual is not None) is bool(expected.get("exists"))
        if "equals" in expected:
            return _state_predicate_hit(actual, expected.get("equals"))
        if "in" in expected and isinstance(expected.get("in"), list | tuple | set):
            return any(_state_predicate_hit(actual, item) for item in expected["in"])
        if "not" in expected:
            return not _state_predicate_hit(actual, expected.get("not"))
    if isinstance(expected, list | tuple | set):
        return any(_state_predicate_hit(actual, item) for item in expected)
    if isinstance(actual, bool):
        if isinstance(expected, str):
            return actual is (expected.strip().lower() in {"1", "true", "yes", "on"})
        return actual is bool(expected)
    return str(actual).strip().lower() == str(expected).strip().lower()
