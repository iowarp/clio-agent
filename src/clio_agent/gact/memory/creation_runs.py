"""Durable, content-free records of memory creation attempts and their costs."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from clio_agent.arc.store_protocol import ARCStore
from clio_agent.gact.memory.store import CREATION_RUNS

_LOCK = threading.RLock()


def failed_memory_tool_count(messages: Iterable[Any]) -> int:
    """Count explicit failed results from the curator's recorded memory tools."""
    relevant = {"memory_search", "memory_read", "propose_memory_change"}
    return sum(
        1
        for message in messages
        for part in getattr(message, "parts", ())
        if getattr(part, "type", "") == "tool_result"
        and getattr(part, "tool_name", "") in relevant
        and bool(getattr(part, "is_error", False))
    )


def curator_run_outcome(
    task_status: str, proposal_counts: Mapping[str, int], failed_tool_count: int
) -> tuple[str, str]:
    """Keep completed no-op runs, but retain source messages after failed curation."""
    if task_status == "completed" and not any(proposal_counts.values()) and failed_tool_count:
        return "failed", "curator_tool_errors_no_proposals"
    return task_status, "" if task_status == "completed" else f"curator_{task_status}"


def creation_run_id(origin: str, run_key: str) -> str:
    """Return the stable idempotency key for one source/origin attempt."""
    return "mcr_" + sha256(f"{origin}\0{run_key}".encode()).hexdigest()[:24]


def begin_run(
    store: ARCStore,
    *,
    run_id: str,
    origin: str,
    source_session: str = "",
    workspace_id: str = "",
    started_at: str | None = None,
) -> dict[str, Any]:
    """Create an extracting row once, preserving a previous attempt on replay."""
    with _LOCK:
        current = _read(store, run_id)
        if current is not None:
            return current
        row: dict[str, Any] = {
            "id": run_id,
            "origin": origin,
            "source_session": source_session,
            "workspace_id": workspace_id,
            "curator_session": "",
            "handle": "",
            "state": "extracting",
            "reason": "",
            "started_at": started_at or _stamp(),
            "completed_at": "",
            "models": {"extract": None, "curator": None},
            "extract": None,
            "proposal_counts": {"accepted": 0, "pending": 0, "rejected": 0},
            "targets": [],
            "_extract_usage": _empty_usage(known=False),
            "_curator_usage": _empty_usage(known=False),
        }
        _write(store, row)
        return row


def update_run(store: ARCStore, run_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Merge one state transition under the process-local record lock."""
    with _LOCK:
        current = _read(store, run_id)
        if current is None:
            return None
        original = json.dumps(current, sort_keys=True)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key] = {**current[key], **value}
            else:
                current[key] = value
        if json.dumps(current, sort_keys=True) != original:
            _write(store, current)
        return current


def record_extract(store: ARCStore, run_id: str, report: dict[str, Any]) -> None:
    """Persist extractor identity, counts and measured/estimated usage, never facts."""
    raw_model = report.get("model")
    model = (
        {
            "provider_id": str(raw_model.get("provider_id") or ""),
            "model_id": str(raw_model.get("model_id") or ""),
            "source": str(raw_model.get("source") or ""),
        }
        if isinstance(raw_model, dict)
        else None
    )
    raw_usage = report.get("usage")
    extract = {
        "returned": _int(report.get("returned")),
        "kept": _int(report.get("kept")),
        "dropped": {
            "sensitive": _int(raw_dropped.get("sensitive")),
            "uncited": _int(raw_dropped.get("uncited")),
        }
        if isinstance(raw_dropped := report.get("dropped"), dict)
        else {"sensitive": 0, "uncited": 0},
    }
    update_run(
        store,
        run_id,
        {
            "models": {"extract": model},
            "extract": extract,
            "_extract_usage": _normalize_usage(raw_usage),
        },
    )


def proposal_counts(service: Any, origin: str, handle: str) -> dict[str, int]:
    """Count durable proposals for one exact origin and curator assignment."""
    return proposal_summary(service, origin, handle)[0]


def proposal_targets(service: Any, origin: str, handle: str) -> list[str]:
    """Return accepted targets from one exact curator assignment that still exist."""
    return proposal_summary(service, origin, handle)[1]


def proposal_summary(service: Any, origin: str, handle: str) -> tuple[dict[str, int], list[str]]:
    """Read counts and safe accepted targets for one exact origin/handle pair."""
    if not handle:
        return {"accepted": 0, "pending": 0, "rejected": 0}, []
    key = (origin, handle)
    return proposal_summaries(service, {key})[key]


def proposal_summaries(
    service: Any, runs: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[dict[str, int], list[str]]]:
    """Read counts and safe targets for exact origin/handle pairs in one scan."""
    from clio_agent.gact.memory.proposals import Proposal  # noqa: PLC0415
    from clio_agent.gact.memory.store import PROPOSALS  # noqa: PLC0415

    counts_by_run = {run: {"accepted": 0, "pending": 0, "rejected": 0} for run in runs}
    targets_by_run: dict[tuple[str, str], set[str]] = {run: set() for run in counts_by_run}
    for name in service.store.scan_names(PROPOSALS):
        raw = service.store.get(PROPOSALS, name)
        if raw is None:
            continue
        proposal = Proposal(**json.loads(raw))
        key = (proposal.origin, proposal.handle)
        if key not in counts_by_run:
            continue
        counts = counts_by_run[key]
        if proposal.status in counts:
            counts[proposal.status] += 1
        if proposal.status == "accepted" and service.get(proposal.target) is not None:
            targets_by_run[key].add(proposal.target)
    return {run: (counts, sorted(targets_by_run[run])) for run, counts in counts_by_run.items()}


def list_run_rows(
    store: ARCStore, *, limit: int = 50, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Read a stable newest-first page, with an opaque timestamp/id continuation."""
    rows, next_cursor = load_run_rows(store, limit=limit, cursor=cursor)
    return [public_run(row) for row in rows], next_cursor


def load_run_rows(
    store: ARCStore,
    *,
    limit: int = 50,
    cursor: str | None = None,
    extra_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read stored rows before refreshing live task/session status."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    before = _decode_cursor(cursor) if cursor else None
    existing_names = set(store.scan_names(CREATION_RUNS))
    rows = []
    for row in extra_rows or []:
        run_id = str(row.get("id") or "")
        ordering = (str(row.get("started_at") or ""), run_id)
        if run_id not in existing_names and (before is None or ordering < before):
            rows.append(row)
    for name in existing_names:
        stored = _read(store, name)
        if stored is not None:
            ordering = (str(stored.get("started_at") or ""), str(stored.get("id") or ""))
            if before is None or ordering < before:
                rows.append(stored)
    rows_by_id = {str(row.get("id") or ""): row for row in rows}
    rows = list(rows_by_id.values())
    rows.sort(
        key=lambda row: (str(row.get("started_at") or ""), str(row.get("id") or "")),
        reverse=True,
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    return page, next_cursor


def legacy_run_rows(sessions: list[Any], store: ARCStore) -> list[dict[str, Any]]:
    """Project pre-ledger IN_FLIGHT/LAST_PASS evidence without inventing times or usage."""
    from clio_agent.gact.memory.background import IN_FLIGHT, LAST_PASS  # noqa: PLC0415

    existing = set(store.scan_names(CREATION_RUNS))
    rows: list[dict[str, Any]] = []
    for session in sessions:
        metadata = getattr(session, "metadata", None) or {}
        inflight = metadata.get(IN_FLIGHT)
        last_pass = metadata.get(LAST_PASS)
        if isinstance(last_pass, dict):
            row = _legacy_session_row(session, last_pass, current=False)
            if row["id"] not in existing:
                rows.append(row)
        if isinstance(inflight, dict):
            row = _legacy_session_row(session, inflight, current=True)
            if row["id"] not in existing:
                rows.append(row)
    return rows


def _legacy_session_row(session: Any, state: dict[str, Any], *, current: bool) -> dict[str, Any]:
    """Project one historical source-session state without fabricating timing data."""
    read_to = str(state.get("read_to") or "")
    origin = str(state.get("origin") or f"session:{session.id}")
    run_id = str(state.get("run_id") or creation_run_id(origin, f"{session.id}:{read_to}"))
    raw_report = state.get("extract")
    report = raw_report if isinstance(raw_report, dict) else {}
    status = str(state.get("status") or "running")
    no_candidates = status == "no_candidates"
    return {
        "id": run_id,
        "origin": origin,
        "source_session": str(session.id),
        "workspace_id": str(getattr(session, "workspace_id", "") or ""),
        "curator_session": str(state.get("session") or ""),
        "handle": str(state.get("handle") or ""),
        "state": "running" if current else ("completed" if no_candidates else status),
        "reason": "no_candidates" if no_candidates else str(state.get("reason") or ""),
        "started_at": "",
        "completed_at": "" if current else str(state.get("at") or ""),
        "models": {"extract": _model(report.get("model")), "curator": None},
        "extract": _extract_view(report),
        "proposal_counts": {"accepted": 0, "pending": 0, "rejected": 0},
        "targets": [],
        "_extract_usage": _normalize_usage(report.get("usage")),
        "_curator_usage": known_zero_usage() if no_candidates else _empty_usage(known=False),
    }


def public_run(row: dict[str, Any]) -> dict[str, Any]:
    """Project private bookkeeping into the stable API row; no fact text is stored."""
    extract_usage = _normalize_usage(row.get("_extract_usage"))
    curator_usage = _normalize_usage(row.get("_curator_usage"))
    if (
        row.get("reason") == "no_candidates"
        and not row.get("curator_session")
        and not row.get("handle")
    ):
        # Older rows omitted token-known markers for a curator phase that never ran.
        curator_usage = known_zero_usage()
    input_known = bool(extract_usage["input_known"] and curator_usage["input_known"])
    output_known = bool(extract_usage["output_known"] and curator_usage["output_known"])
    cost_known = bool(extract_usage["cost_known"] and curator_usage["cost_known"])
    cost = (
        round(float(extract_usage["cost_usd"] or 0) + float(curator_usage["cost_usd"] or 0), 8)
        if cost_known
        else None
    )
    raw_models = row.get("models")
    models = raw_models if isinstance(raw_models, dict) else {}
    extract = row.get("extract")
    return {
        "id": str(row.get("id") or ""),
        "origin": str(row.get("origin") or ""),
        "source_session": str(row.get("source_session") or ""),
        "workspace_id": str(row.get("workspace_id") or ""),
        "curator_session": str(row.get("curator_session") or ""),
        "handle": str(row.get("handle") or ""),
        "state": str(row.get("state") or "unknown"),
        "reason": str(row.get("reason") or ""),
        "started_at": str(row.get("started_at") or ""),
        "completed_at": str(row.get("completed_at") or ""),
        "models": {"extract": models.get("extract"), "curator": models.get("curator")},
        "usage": {
            "input_tokens": (
                extract_usage["input_tokens"] + curator_usage["input_tokens"]
                if input_known
                else None
            ),
            "output_tokens": (
                extract_usage["output_tokens"] + curator_usage["output_tokens"]
                if output_known
                else None
            ),
            "cost_usd": cost,
            "cost_known": cost_known,
        },
        "extract": extract if isinstance(extract, dict) else None,
        "proposal_counts": _counts(row.get("proposal_counts")),
        "targets": _strings(row.get("targets")),
    }


def _model(raw_model: Any) -> dict[str, str] | None:
    if not isinstance(raw_model, dict):
        return None
    return {
        "provider_id": str(raw_model.get("provider_id") or ""),
        "model_id": str(raw_model.get("model_id") or ""),
        "source": str(raw_model.get("source") or ""),
    }


def _extract_view(
    report: dict[str, Any], dropped: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if not report:
        return None
    raw_dropped = dropped if dropped is not None else report.get("dropped")
    raw_dropped = raw_dropped if isinstance(raw_dropped, dict) else {}
    return {
        "returned": _int(report.get("returned")),
        "kept": _int(report.get("kept")),
        "dropped": {
            "sensitive": _int(raw_dropped.get("sensitive")),
            "uncited": _int(raw_dropped.get("uncited")),
        },
    }


def _read(store: ARCStore, name: str) -> dict[str, Any] | None:
    raw = store.get(CREATION_RUNS, name)
    if raw is None:
        return None
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else None


def _write(store: ARCStore, row: dict[str, Any]) -> None:
    store.put(CREATION_RUNS, str(row["id"]), json.dumps(row, sort_keys=True).encode())


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _empty_usage(*, known: bool) -> dict[str, Any]:
    return {
        "input_tokens": 0 if known else None,
        "output_tokens": 0 if known else None,
        "input_known": known,
        "output_known": known,
        "cost_usd": 0.0 if known else None,
        "cost_known": known,
    }


def known_zero_usage() -> dict[str, Any]:
    """Describe a phase known not to have made any model calls."""
    return _empty_usage(known=True)


def _normalize_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_usage(known=False)
    known = bool(value.get("cost_known"))
    cost = value.get("cost_usd")
    input_value = value.get("input_tokens", value.get("input"))
    output_value = value.get("output_tokens", value.get("output"))
    input_known = bool(value.get("input_known", input_value is not None))
    output_known = bool(value.get("output_known", output_value is not None))
    return {
        "input_tokens": _int(input_value) if input_known else None,
        "output_tokens": _int(output_value) if output_known else None,
        "input_known": input_known,
        "output_known": output_known,
        "cost_usd": float(cost) if known and isinstance(cost, (int, float)) else None,
        "cost_known": known,
    }


def _counts(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {key: _int(source.get(key)) for key in ("accepted", "pending", "rejected")}


def _strings(value: Any) -> list[str]:
    """Keep only explicit strings from optional legacy target metadata."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _encode_cursor(row: dict[str, Any]) -> str:
    payload = json.dumps([str(row.get("started_at") or ""), str(row.get("id") or "")]).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(decoded)
        if isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value):
            return value[0], value[1]
    except (ValueError, json.JSONDecodeError):
        pass
    raise ValueError("invalid memory creation-run cursor")


__all__ = [
    "begin_run",
    "creation_run_id",
    "list_run_rows",
    "load_run_rows",
    "legacy_run_rows",
    "public_run",
    "proposal_counts",
    "proposal_summary",
    "proposal_summaries",
    "proposal_targets",
    "record_extract",
    "update_run",
]
