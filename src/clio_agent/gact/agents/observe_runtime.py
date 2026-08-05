"""Cursor-based incremental child observation — the OBSERVE posture (#1000).

The owner module for ``observe_agent_tasks``, the read-only sibling of
``check_agent_tasks``. It mirrors the SEMANTICS of clio-relay's ``relay_observe``
(``clio-relay/src/clio_relay/mcp_server.py::_observe_job``) adapted to clio-agent's
spawn substrate:

* **Cursor** — read a child's event stream incrementally from a resumable,
  monotonic cursor (the process-global event id); two sequential observes never
  miss and never re-read (``EventBus.session_events_since``).
* **Pattern** — an optional regex that makes the call return EARLY the moment new
  event text matches, so the parent acts on intermediate evidence (a typed
  ``workflow_state`` landing, a stage completing) instead of waiting for the
  child's terminal.
* **Non-consuming** — observation reads only. It never touches ``notify_pending`` /
  ``consumed_at`` and never emits a delegation terminal; the delegation stays open
  until ``wait_agent_tasks`` / ``check_agent_tasks`` / next-turn injection collects
  it (the exactly-once contract is unchanged — observe is repeatable).

The three postures after a fire-and-forget spawn: WAIT (blocking terminal-seeking,
``wait_agent_tasks``), OBSERVE (non-blocking incremental progress-watching, HERE),
CONTINUE (observe-later injection, ``enrichment``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# --- Bounds (defensive; never run user regex over unbounded raw payloads) ----- #

#: Default number of raw events surfaced per observe call when the caller passes no
#: ``limit`` (a curated page; the model reads a page, advances ``next_cursor``, reads
#: on). A batch-wide cap over the id-sorted stream so cursor semantics stay coherent.
DEFAULT_OBSERVE_LIMIT = 40
#: Hard ceiling on ``limit`` — a chatty child cannot dump an unbounded page.
MAX_OBSERVE_LIMIT = 200
#: Max characters kept in ONE curated excerpt; the overflow is dropped with a
#: ``… [+N chars]`` truncation note appended (bounded rows, never a raw dump).
OBSERVE_EXCERPT_MAX_CHARS = 600
#: The regex is only ever run over this many characters of an already-bounded
#: per-event excerpt+summary — the bounded match window (never the raw payload).
OBSERVE_MATCH_MAX_CHARS = 4000
#: Poll interval while blocking for a pattern match (relay's ``poll_seconds``
#: analogue; short so intra-turn evidence surfaces promptly).
OBSERVE_POLL_SECONDS = 0.2
#: Hard ceiling on how long a single observe call may block (a runaway backstop; a
#: caller asking for more is capped, never allowed to wedge the turn).
MAX_OBSERVE_TIMEOUT_S = 120.0

# --- Event-family curation ----------------------------------------------------- #
# The child's bus history already carries only the SSE-served ReAct atoms
# (``event_reaches_ui``): react steps, the extract's typed landing, delegation
# stage transitions, skill loads, memory searches, plus any failure. From those we
# curate the semantically-useful families and DROP the rest as noise (raw message
# deltas, status_changed, heartbeats never reach here — they are not ``semantic.event``).

FAMILY_BY_EVENT_TYPE: dict[str, str] = {
    "react.step.completed": "react.step",
    "expert.extract.completed": "extract",
    "expert.response.completed": "response",
    "expert.lifecycle.started": "lifecycle",
    "blueprint.delegation.started": "delegation",
    "blueprint.delegation.completed": "delegation",
    "blueprint.delegation.failed": "delegation",
    "blueprint.delegation.parent_resumed": "delegation",
    "delegation.started": "delegation",
    "delegation.completed": "delegation",
    "delegation.failed": "delegation",
    "delegation.parent_resumed": "delegation",
    "skill.loaded": "skill",
    "memory.search.completed": "memory",
}

_TERMINAL_STATUSES_FOR_STATE = frozenset({"failed", "error", "cancelled"})


def _bounded(text: str, limit: int = OBSERVE_EXCERPT_MAX_CHARS) -> tuple[str, bool]:
    """Return ``(text, truncated)`` bounded to ``limit`` characters, appending a
    ``… [+N chars]`` note when it overflows (so a huge event text never dumps raw)."""

    if len(text) <= limit:
        return text, False
    dropped = len(text) - limit
    return f"{text[:limit]}… [+{dropped} chars]", True


def _jsonish(value: Any) -> str:
    """Compact, deterministic string form of a payload value for an excerpt."""

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _excerpt_for_family(family: str, body: Mapping[str, Any], inner: Mapping[str, Any]) -> tuple[str, bool]:
    """Compose ONE bounded excerpt for a curated family from the semantic payload.

    Curation is deliberate — a react step surfaces its thought + the tool it called
    + the observation; an extract surfaces its output + the typed structured landing;
    a delegation surfaces its stage + returned output. Everything else falls back to
    the event summary. Raw deltas are never surfaced.
    """

    parts: list[str] = []
    if family == "react.step":
        thought = str(inner.get("thought") or "").strip()
        tool = str(inner.get("tool_name") or "").strip()
        observation = inner.get("observation")
        if thought:
            parts.append(f"thought: {thought}")
        if tool:
            args = inner.get("tool_args")
            args_s = _jsonish(args) if args else ""
            parts.append(f"tool: {tool}({args_s})")
        if observation not in (None, "", {}, []):
            parts.append(f"obs: {_jsonish(observation)}")
    elif family == "extract":
        output = str(inner.get("output") or "").strip()
        if output:
            parts.append(f"output: {output}")
        structured = inner.get("structured")
        if isinstance(structured, Mapping) and structured:
            parts.append(f"structured: {_jsonish(structured)}")
    elif family == "delegation":
        stage = str(inner.get("stage") or body.get("event_type") or "").strip()
        if stage:
            parts.append(stage)
        deleg_output = inner.get("output")
        if deleg_output not in (None, "", {}, []):
            parts.append(f"output: {_jsonish(deleg_output)}")
        ws = inner.get("workflow_state")
        if isinstance(ws, Mapping) and ws:
            parts.append(f"workflow_state: {_jsonish(ws)}")
    if not parts:
        parts.append(str(body.get("summary") or "").strip())
    return _bounded(" | ".join(p for p in parts if p))


def curate_event(event: Any) -> dict[str, Any] | None:
    """Curate ONE bus event into a bounded observe row, or ``None`` when it is noise.

    Only ``semantic.event`` bus records are candidates (raw ``message.*`` deltas,
    ``status_changed``, and transient heartbeats are dropped). Within those, only a
    known useful family OR any failure/cancellation status survives.
    """

    if getattr(event, "type", "") != "semantic.event":
        return None
    body = getattr(event, "payload", None) or {}
    if not isinstance(body, Mapping):
        return None
    event_type = str(body.get("event_type", ""))
    status = str(body.get("status", ""))
    family = FAMILY_BY_EVENT_TYPE.get(event_type)
    if family is None:
        if status.strip().lower() not in _TERMINAL_STATUSES_FOR_STATE:
            return None
        family = "status"
    inner_raw = body.get("payload")
    inner: Mapping[str, Any] = inner_raw if isinstance(inner_raw, Mapping) else {}
    excerpt, truncated = _excerpt_for_family(family, body, inner)
    summary, summary_truncated = _bounded(str(body.get("summary", "")))
    row: dict[str, Any] = {
        "seq": int(getattr(event, "id", 0)),
        "family": family,
        "event_type": event_type,
        "status": status,
        "summary": summary,
        "excerpt": excerpt,
    }
    if truncated or summary_truncated:
        row["truncated"] = True
    return row


def _event_matches(compiled: "re.Pattern[str]", row: Mapping[str, Any]) -> bool:
    """Run the compiled regex over the row's BOUNDED excerpt+summary only — the
    bounded match window. The excerpt is already ≤ ``OBSERVE_EXCERPT_MAX_CHARS`` and
    we cap the combined text at ``OBSERVE_MATCH_MAX_CHARS`` before matching so the
    user regex never runs over an unbounded raw payload."""

    text = f"{row.get('summary', '')}\n{row.get('excerpt', '')}"[:OBSERVE_MATCH_MAX_CHARS]
    return compiled.search(text) is not None


# --- workflow_state snapshot --------------------------------------------------- #


def _find_workflow_state(obj: Any, depth: int = 0) -> dict[str, Any]:
    """Depth-bounded recursive search for a non-empty typed ``workflow_state`` mapping
    inside a semantic payload (extract carries it under ``structured``; delegation
    carries it top-level). Returns ``{}`` when none is present."""

    if depth > 4 or not isinstance(obj, Mapping):
        return {}
    ws = obj.get("workflow_state")
    if isinstance(ws, Mapping) and ws:
        return dict(ws)
    structured = obj.get("structured")
    if isinstance(structured, Mapping):
        inner = structured.get("workflow_state")
        if isinstance(inner, Mapping) and inner:
            return dict(inner)
    for value in obj.values():
        if isinstance(value, Mapping):
            found = _find_workflow_state(value, depth + 1)
            if found:
                return found
    return {}


def latest_workflow_state(app: Any, task: Any) -> dict[str, Any]:
    """The child's CURRENT typed ``workflow_state`` snapshot (point-in-time, cursor-
    independent). A terminal child's authoritative state is on its result; a still-
    running child's is derived from the most recent typed landing in its event
    stream (the same store, no fifth history)."""

    result = task.result or {}
    ws = result.get("workflow_state")
    if isinstance(ws, Mapping) and ws:
        return dict(ws)
    bus = getattr(app.state, "bus", None)
    if bus is None:
        return {}
    latest: dict[str, Any] = {}
    for event in bus.session_events_since(task.child_session_id, cursor=1):
        if getattr(event, "type", "") != "semantic.event":
            continue
        body = getattr(event, "payload", None) or {}
        if not isinstance(body, Mapping):
            continue
        found = _find_workflow_state(body.get("payload"))
        if found:
            latest = found  # keep the highest-id (most recent) non-empty landing
    return latest


# --- read-once + poll loop ----------------------------------------------------- #


def _no_more_events(resolved: Mapping[str, Any]) -> bool:
    """True when every REQUESTED task is unknown or terminal — no further events will
    land, so blocking for a pattern match is pointless (return promptly)."""

    return all(task is None or task.is_terminal for task in resolved.values())


def _read_once(
    app: Any,
    resolved: Mapping[str, Any],
    requested: list[str],
    *,
    cursor: int,
    limit: int,
    compiled: "re.Pattern[str] | None",
    include_state: bool,
) -> dict[str, Any]:
    """One cursor read: gather every requested child's events with ``id >= cursor``,
    id-sort into a single coherent stream, cap at ``limit`` (batch-wide so the shared
    monotonic cursor never gaps or repeats), curate into per-task rows, and compute a
    single ``next_cursor`` = last-included id + 1."""

    bus = app.state.bus
    candidates: list[tuple[Any, str]] = []
    for tid in requested:
        task = resolved.get(tid)
        if task is None:
            continue
        for event in bus.session_events_since(task.child_session_id, cursor=cursor):
            candidates.append((event, tid))
    candidates.sort(key=lambda pair: pair[0].id)
    events_truncated = len(candidates) > limit
    included = candidates[:limit]
    next_cursor = included[-1][0].id + 1 if included else cursor

    per_task_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    per_task_matched: dict[str, bool] = defaultdict(bool)
    any_match = False
    for event, tid in included:
        curated = curate_event(event)
        if curated is None:
            continue  # noise consumed by the cursor but never surfaced (no re-read)
        if compiled is not None and _event_matches(compiled, curated):
            curated["matched"] = True
            per_task_matched[tid] = True
            any_match = True
        per_task_events[tid].append(curated)

    tasks_out: list[dict[str, Any]] = []
    for tid in requested:
        task = resolved.get(tid)
        if task is None:
            tasks_out.append({"task_id": tid, "error": "unknown_task"})
            continue
        row: dict[str, Any] = {
            "task_id": tid,
            "run_index": task.run_index,
            "status": task.status,
            "new_events": per_task_events.get(tid, []),
            "next_cursor": next_cursor,
            "matched": per_task_matched.get(tid, False),
        }
        if include_state:
            row["workflow_state"] = latest_workflow_state(app, task)
        tasks_out.append(row)

    return {
        "tasks": tasks_out,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "limit": limit,
        "matched": any_match,
        "events_truncated": events_truncated,
    }


def observe_agent_tasks_impl(
    app: Any,
    *,
    task_ids: list[str] | None,
    cursor: int = 1,
    limit: int = DEFAULT_OBSERVE_LIMIT,
    pattern: str | None = None,
    include_state: bool = True,
    timeout_s: float | None = None,
) -> str:
    """Implementation of the ``observe_agent_tasks`` tool (see the module docstring).

    Blocking behaviour:
    * ``timeout_s=None`` — pure non-blocking read (cursor semantics only).
    * ``timeout_s`` + ``pattern`` — block up to ``timeout_s``, but return EARLY the
      moment a new event's bounded text matches the regex (poll every
      ``OBSERVE_POLL_SECONDS``); on timeout return the events so far, ``matched=false``.
    * ``timeout_s`` without ``pattern`` — block up to ``timeout_s`` watching for
      progress, then return the accumulated window (short-circuits early once every
      requested child is terminal — no more events can land).

    Never consumes: no ``notify_pending``/``consumed_at`` mutation, no delegation
    terminal emission (repeatable; ``wait``/``check``/injection keep exactly-once).
    """

    registry = app.state.agent_task_registry

    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = DEFAULT_OBSERVE_LIMIT
    limit_i = max(1, min(limit_i, MAX_OBSERVE_LIMIT))
    try:
        cursor_i = max(1, int(cursor))
    except (TypeError, ValueError):
        cursor_i = 1

    compiled: "re.Pattern[str] | None" = None
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            # Typed error row on an invalid regex (never crash the turn, never silent).
            logger.warning("observe_agent_tasks invalid pattern=%r reason=%s", pattern, exc)
            return json.dumps(
                {
                    "error": "invalid_pattern",
                    "message": str(exc),
                    "pattern": str(pattern),
                    "tasks": [],
                },
                sort_keys=True,
            )

    requested = [str(t) for t in (task_ids or [])]
    resolved: dict[str, Any] = {tid: registry.get(tid) for tid in requested}

    blocking = timeout_s is not None
    deadline = 0.0
    if blocking:
        budget = min(max(0.0, float(timeout_s or 0.0)), MAX_OBSERVE_TIMEOUT_S)
        deadline = time.monotonic() + budget

    while True:
        result = _read_once(
            app,
            resolved,
            requested,
            cursor=cursor_i,
            limit=limit_i,
            compiled=compiled,
            include_state=include_state,
        )
        if not blocking:
            return json.dumps(result, sort_keys=True, default=str)
        if compiled is not None and result["matched"]:
            return json.dumps(result, sort_keys=True, default=str)
        remaining = deadline - time.monotonic()
        if remaining <= 0.0 or _no_more_events(resolved):
            return json.dumps(result, sort_keys=True, default=str)
        time.sleep(min(OBSERVE_POLL_SECONDS, remaining))
        # Refresh task records so a running→terminal transition is seen next poll.
        resolved = {tid: registry.get(tid) for tid in requested}


def build_observe_tool() -> Any:
    """Build the ``observe_agent_tasks`` dspy.Tool (the OBSERVE posture, #1000).

    Bound into the spawn-runtime toolset (present whenever the spawn tools are — same
    declared-children gating). It reads only by ``task_id`` (no requesting-expert
    binding), resolving the active app/session from the runtime context at call time.
    """

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def observe_agent_tasks(
        task_ids: list[str],
        cursor: int = 1,
        limit: int = DEFAULT_OBSERVE_LIMIT,
        pattern: str | None = None,
        include_state: bool = True,
        timeout_s: float | None = None,
    ) -> str:
        """Watch spawned children's progress INCREMENTALLY without consuming them.

        Reads each child's event stream from a resumable ``cursor`` (start at 1; pass
        the returned ``next_cursor`` next time — you never miss or re-read an event)
        and returns per-task rows: bounded excerpts of the useful events (react
        thoughts + tool calls, the child's typed workflow_state landings, delegation
        stage transitions), the child's current ``workflow_state`` snapshot, and its
        status. Unlike ``wait``/``check`` this does NOT collect the child — the
        delegation stays open and you can observe again. Use it to ACT on intermediate
        evidence while the child keeps running. Pass ``pattern`` (a regex) with a
        ``timeout_s`` to return the MOMENT matching evidence appears (e.g. a station
        id landing) instead of waiting for the child to finish."""

        app = _ctx.active_app()
        if app is None or not _ctx.active_session_id():
            raise RuntimeError("observe_agent_tasks requires an active CLIO app/session context")
        return observe_agent_tasks_impl(
            app,
            task_ids=task_ids,
            cursor=cursor,
            limit=limit,
            pattern=pattern,
            include_state=include_state,
            timeout_s=timeout_s,
        )

    return native_tool(
        observe_agent_tasks,
        name="observe_agent_tasks",
        desc=observe_agent_tasks.__doc__,
        title="Observe agent tasks",
        args={
            "task_ids": {
                "type": "array",
                "description": "Task ids (from spawn) whose progress to observe.",
            },
            "cursor": {
                "type": "integer",
                "description": (
                    "Resume point: start at 1, then pass the returned next_cursor "
                    "so each observe reads only NEW events (never misses/re-reads)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max events to return this call (bounded page; default 40).",
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Optional regex — with timeout_s, return the MOMENT a new "
                    "event's text matches (e.g. an id landing), not at timeout."
                ),
            },
            "include_state": {
                "type": "boolean",
                "description": "Include each child's current typed workflow_state snapshot.",
            },
            "timeout_s": {
                "type": "number",
                "description": (
                    "Omit for a non-blocking read. Set to block up to this many "
                    "seconds watching for progress / a pattern match."
                ),
            },
        },
    )
