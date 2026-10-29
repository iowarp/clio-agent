"""An Observation's sources: the recorded calls it cites, and what the record says they did.

The model cites calls (``call:<session>/<call_id>``, the reference every tool result
carries); the log copies the facts from the records that already exist and never
from the model's prose:

* the call's transcript rows (``tool_call`` / ``tool_result`` parts, keyed by the tool
  observer's ``call_id``): tool, arguments, start and end time, and a digest of the
  result, plus the authoring session's agent and model;
* the artifact registry's ``TransformRecord`` for the same ``call_id``: the artifact
  versions the call used and generated, each with its SHA-256.

A cited call must be in the author's own transcript (which holds a fork's copied
history) or in the transcript of a session the author delegated to; otherwise the
entry is rejected (``lineage_call_unknown``, ``lineage_call_outside_branch``). A
measurement may name a field of a cited call's structured result instead of a value,
and the log copies the value (``measurement_field_unknown`` when the result has no
such field).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from clio_agent.gact.science_log.entries import EntryRejected

CALL_PREFIX = "call:"


def call_reference(session_id: str, call_id: str) -> str:
    """The reference a tool result carries and an Observation cites."""

    return f"{CALL_PREFIX}{session_id}/{call_id}"


def parse_reference(reference: str, default_session: str) -> tuple[str, str]:
    """``(session, call_id)`` from ``call:<session>/<call_id>``, ``call:<id>`` or ``<id>``."""

    text = str(reference or "").strip().removeprefix(CALL_PREFIX)
    session, _, call_id = text.rpartition("/")
    return (session or default_session), call_id


def _parts(app: Any, session_id: str) -> list[Any]:
    from clio_agent.gact.tool_observer import _session_turn_transcript  # noqa: PLC0415

    parts = [part for message in app.state.messages.get(session_id, []) for part in message.parts]
    transcript = _session_turn_transcript(app, session_id)
    if transcript is not None:
        parts += list(transcript.snapshot())
    return parts


def find_call(app: Any, session_id: str, call_id: str) -> dict[str, Any] | None:
    """What ``session_id``'s transcript records of ``call_id``, or ``None``."""

    call: dict[str, Any] | None = None
    for part in _parts(app, session_id):
        if getattr(part, "call_id", "") != call_id:
            continue
        if part.type == "tool_call" and call is None:
            call = {
                "call_id": call_id,
                "tool": part.tool_name,
                "args": dict(getattr(part, "input", None) or {}),
                "started_at": str((part.metadata or {}).get("started_at") or ""),
            }
        elif part.type == "tool_result" and call is not None:
            text = "".join(
                str(
                    getattr(block, "text", "")
                    or (block.get("text", "") if isinstance(block, dict) else "")
                )
                for block in (getattr(part, "content", None) or [])
            ) or str(getattr(part, "text", "") or "")
            # The structured result is the same in the live turn and the stored
            # transcript (its text preview is not), so it is what is read and hashed.
            structured = getattr(part, "structured_content", None)
            if structured is not None:
                text = json.dumps(structured, sort_keys=True, separators=(",", ":"), default=str)
            call.update(
                completed_at=str((part.metadata or {}).get("completed_at") or ""),
                result_text=text,
                result_sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
    return call


def _subtree_sessions(app: Any, session_id: str) -> set[str]:
    """The author and every session it delegated to, at any depth."""

    history = app.state.task_history
    sessions = {session_id}
    assignment = history.latest_assignment(session_id)
    if assignment is not None:
        for handle in history.descendant_handles(assignment.handle):
            task = history.task_snapshot(handle)
            if task is not None and task.kind in {"Session", "Subagent"}:
                sessions.add(task.owner.session_id)
    return sessions


def _transform(app: Any, call_id: str) -> Any:
    try:
        from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415

        return get_registry(app).get_transform(call_id)
    except (AttributeError, RuntimeError):
        return None


def _artifacts(app: Any, call_id: str) -> list[dict[str, Any]]:
    record = _transform(app, call_id)
    if record is None:
        return []
    rows = []
    for direction in ("used", "generated"):
        for edge in getattr(record, direction, None) or []:
            row = edge.model_dump() if hasattr(edge, "model_dump") else dict(edge)
            rows.append({"direction": direction, **row})
    instrument = getattr(record, "instrument", None)
    script_hash = getattr(instrument, "script_hash", None)
    if script_hash:
        rows.append(
            {"direction": "script", "sha256": script_hash, "cmd": getattr(instrument, "cmd", "")}
        )
    return rows


def _field(value: Any, path: str) -> Any:
    """``a.b.0.c`` (or a JSON pointer ``/a/b/0/c``) inside a parsed result."""

    current = value
    # ``a.b.0.c``, ``/a/b/0/c`` and ``a.b[0].c`` all name the same field.
    path = path.replace("[", ".").replace("]", "")
    for key in [k for k in path.replace("/", ".").split(".") if k]:
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
            current = current[int(key)]
        else:
            raise KeyError(path)
    return current


def observation_sources(app: Any, author: str, fields: dict[str, Any]) -> dict[str, Any]:
    """``fields`` with ``lineage`` and ``artifacts`` filled from the cited calls and each
    sourced measurement's value copied from its call's result."""

    if not fields.get("from_calls"):
        raise EntryRejected(
            "lineage_missing", "an observation cites the recorded calls behind it in from_calls"
        )
    allowed = _subtree_sessions(app, author)
    session = app.state.sessions.get(author)
    agent = str((getattr(session, "agent", None) or {}).get("id") or "")
    model = getattr(session, "model", None) or {}
    calls: dict[str, dict[str, Any]] = {}
    for reference in fields["from_calls"]:
        named, call_id = parse_reference(reference, author)
        if not call_id:
            raise EntryRejected("lineage_call_unknown", f"{reference!r} names no call")
        found = None
        for holder in (named, *sorted(allowed - {named})):
            if holder in allowed and (found := find_call(app, holder, call_id)) is not None:
                named = holder
                break
        if found is None:
            other = find_call(app, named, call_id) if named not in allowed else None
            raise EntryRejected(
                "lineage_call_outside_branch" if other is not None else "lineage_call_unknown",
                f"{reference!r} is "
                + (
                    "a call of another branch: cite calls you or your delegates made"
                    if other is not None
                    else "not a recorded call"
                ),
            )
        owner = app.state.sessions.get(named)
        record = _transform(app, call_id)
        recorded = getattr(getattr(record, "environment", None), "model_id", "") or ""
        calls[call_id] = {
            **found,
            "reference": call_reference(named, call_id),
            "session": named,
            "agent": str((getattr(owner, "agent", None) or {}).get("id") or agent),
            # The model that ran the call, as recorded with it; else the session's pin.
            "model": str(recorded)
            or "/".join(
                str((getattr(owner, "model", None) or model).get(key) or "")
                for key in ("provider_id", "model_id")
            ).strip("/"),
        }
    measurements = []
    for row in fields.get("measurements") or []:
        source = row.get("source")
        if source:
            _, call_id = parse_reference(source["call"], author)
            call = calls.get(call_id)
            if call is None:
                raise EntryRejected(
                    "measurement_call_uncited",
                    f"measurement {row['metric']!r} names a call the observation does not cite",
                )
            try:
                parsed = json.loads(call.get("result_text") or "null")
                row = {**row, "value": _field(parsed, source["field"])}
            except (KeyError, ValueError) as exc:
                raise EntryRejected(
                    "measurement_field_unknown",
                    f"{source['call']} returned no field {source['field']!r}",
                ) from exc
        measurements.append(row)
    lineage = [
        {key: value for key, value in call.items() if key != "result_text"}
        for call in calls.values()
    ]
    artifacts = [
        {"call_id": call_id, **artifact}
        for call_id in calls
        for artifact in _artifacts(app, call_id)
    ]
    return {**fields, "measurements": measurements, "lineage": lineage, "artifacts": artifacts}


__all__ = ["CALL_PREFIX", "call_reference", "find_call", "observation_sources", "parse_reference"]
