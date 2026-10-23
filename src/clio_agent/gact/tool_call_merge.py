"""Merge wrapper and live-observer tool evidence without losing invocation identity."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.evidence import _bounded_tool_call_result
from clio_agent.gact.tool_event_identity import _tool_call_event_key, _tool_call_name_args_key


def _tool_call_has_result_evidence(call: Mapping[str, Any]) -> bool:
    """Return whether a tool-call row carries auditable result evidence."""

    for key in ("result", "observation", "output", "response", "result_preview"):
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _normalize_tool_call_row(call: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a tool-call row while preserving bounded result evidence."""

    row: dict[str, Any] = {}
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        row["call_id"] = call_id
    name = call.get("name") or call.get("tool")
    if name:
        row["name"] = str(name)
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    if args is not None:
        row["args"] = args
    for key in ("ok", "duration_ms", "cached", "error", "telemetry_source"):
        if key in call:
            row[key] = call[key]
    for key in ("result", "observation", "output", "response", "result_preview"):
        if key not in call:
            continue
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "result":
            row["result"] = _bounded_tool_call_result(value)
        else:
            row[key] = _bounded_tool_call_result(value)
        break
    if row and "telemetry_source" not in row:
        row["telemetry_source"] = "posthoc_prediction"
    return row


def _merge_tool_call_rows(
    primary_rows: list[dict[str, Any]],
    supplemental_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tool-call telemetry without dropping richer result evidence."""

    merged: list[dict[str, Any]] = [_normalize_tool_call_row(row) for row in primary_rows if row]
    by_key: dict[tuple[str, str], list[int]] = {}
    by_name_args: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(merged):
        by_key.setdefault(_tool_call_event_key(row), []).append(index)
        by_name_args.setdefault(_tool_call_name_args_key(row), []).append(index)

    for raw_supplemental in supplemental_rows:
        supplemental = _normalize_tool_call_row(raw_supplemental)
        if not supplemental:
            continue
        key = _tool_call_event_key(supplemental)
        candidate_index: int | None = None
        supplemental_has_result = _tool_call_has_result_evidence(supplemental)
        supplemental_ok = supplemental.get("ok")
        candidate_indexes = list(by_key.get(key, []))
        if not candidate_indexes:
            fallback_indexes = by_name_args.get(_tool_call_name_args_key(supplemental), [])
            if supplemental_has_result:
                fallback_indexes = [
                    index for index in fallback_indexes if merged[index].get("ok") is not False
                ]
            if fallback_indexes:
                candidate_indexes = fallback_indexes
        for index in candidate_indexes:
            existing = merged[index]
            existing_ok = existing.get("ok")
            if key[0] == "__call_id__":
                existing_key = _tool_call_event_key(existing)
                if existing_key[0] == "__call_id__" and existing_key != key:
                    continue
                candidate_index = index
                break
            if supplemental_has_result and existing_ok is False and supplemental_ok is not False:
                continue
            if supplemental_has_result and not _tool_call_has_result_evidence(existing):
                candidate_index = index
                break
            if not supplemental_has_result:
                candidate_index = index
                break
        if candidate_index is None:
            by_key.setdefault(key, []).append(len(merged))
            by_name_args.setdefault(_tool_call_name_args_key(supplemental), []).append(len(merged))
            merged.append(supplemental)
            continue

        existing = merged[candidate_index]
        old_key = _tool_call_event_key(existing)
        for field_name, value in supplemental.items():
            if field_name in {"result", "observation", "output", "response", "result_preview"}:
                if not _tool_call_has_result_evidence(existing):
                    existing[field_name] = value
                continue
            if value in (None, "", [], {}):
                continue
            if field_name not in existing or existing[field_name] in (None, "", [], {}):
                existing[field_name] = value
            elif field_name in {"duration_ms", "cached", "telemetry_source", "ok", "error"}:
                existing[field_name] = value
        new_key = _tool_call_event_key(existing)
        if new_key != old_key and candidate_index not in by_key.get(new_key, []):
            by_key.setdefault(new_key, []).append(candidate_index)
    return merged
