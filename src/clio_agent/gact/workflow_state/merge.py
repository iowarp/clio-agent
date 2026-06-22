"""Pure workflow_state merge/normalize helpers (#714).

Behavior-preserving extraction from ``clio_agent.gact.app``. These helpers are
pure stdlib (``re``, ``pathlib.Path``, ``collections.abc.Mapping``) and call
only each other; they read no contextvars and no module-level app state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _merge_inferred_workflow_state(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
) -> None:
    for key, value in incoming.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_non_empty_mapping(target[key], {str(k): v for k, v in value.items()})
        else:
            target[str(key)] = value


def _value_has_semantic_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set | dict):
        return bool(value)
    return True


def _merge_non_empty_mapping(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Merge without letting empty model fields erase tool provenance."""

    for raw_key, raw_value in incoming.items():
        key = str(raw_key)
        current = target.get(key)
        if not _value_has_semantic_content(raw_value) and _value_has_semantic_content(current):
            continue
        target[key] = raw_value


_UNICODE_PATH_HYPHENS = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "−": "-",
}


def _normalize_pathlike_text(value: str) -> str:
    normalized = value
    for source, replacement in _UNICODE_PATH_HYPHENS.items():
        normalized = normalized.replace(source, replacement)
    return normalized


def _normalize_workflow_state_scalar(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    lowered = key.lower()
    if any(token in lowered for token in ("path", "url", "filepath", "filename", "resource_name")):
        return _normalize_pathlike_text(value)
    return value


def _workflow_status_rank(section: str, value: Mapping[str, Any]) -> int:
    """Return semantic progress rank for workflow-state merge precedence."""

    status = str(value.get("status") or "").strip().lower()
    if section == "acquisition":
        local_path = str(value.get("local_path") or value.get("path") or "").strip()
        if (
            status == "staged"
            and value.get("analysis_ready") is True
            and local_path.startswith(("/", "~"))
            and not Path(local_path).expanduser().is_file()
        ):
            return 1
        if status == "staged" and value.get("analysis_ready") is True:
            return 5
        if status == "staged":
            return 4
        if status == "metadata_only":
            return 3
        if status in {"blocked", "missing"}:
            return 2
        if status:
            return 1
        return 0
    if section == "resource_candidate":
        if status == "selected":
            return 4
        if status == "metadata_only":
            return 3
        if status in {"missing", "blocked"}:
            return 2
        if status:
            return 1
        return 0
    if section in {"profile", "visualization", "artifact", "network_analysis"}:
        if status in {"complete", "completed", "created", "plotted"}:
            return 4
        if status in {"blocked", "missing"}:
            return 2
        if status:
            return 1
        return 0
    if section == "catalog":
        if status in {"candidates_found", "metadata_found"}:
            return 3
        if status == "search_incomplete":
            return 2
        if status in {"no_candidates", "blocked"}:
            return 2
        if status:
            return 1
        return 0
    if section == "resource_discovery":
        if status in {"resource_found", "candidate_found"}:
            return 4
        if status == "search_required":
            return 3
        if status in {"search_exhausted", "blocked"}:
            return 2
        if status:
            return 1
        return 0
    return 0


def _normalize_workflow_state_section(section: str, value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {str(k): _normalize_workflow_state_scalar(str(k), v) for k, v in value.items()}
    if section != "acquisition":
        return normalized
    status = str(normalized.get("status") or "").strip().lower()
    local_path = str(normalized.get("local_path") or normalized.get("path") or "").strip()
    metadata_path = str(normalized.get("metadata_path") or "").strip()
    # A metadata/catalog file is not an analysis-ready data resource. When the
    # analysis-ready local_path is the SAME file the expert recorded as the
    # discovery metadata_path, the expert reused the catalog instead of staging a
    # distinct data resource -- so it cannot be analysis-ready. This compares two
    # typed fields the schema already carries; it hardcodes no domain/file names.
    reused_metadata_as_data = bool(local_path) and local_path == metadata_path
    if normalized.get("analysis_ready") is True and (
        status != "staged" or not local_path or reused_metadata_as_data
    ):
        normalized["analysis_ready"] = False
        if status in {"blocked", "missing", "metadata_only"}:
            normalized["status"] = status
        elif reused_metadata_as_data:
            normalized["status"] = "metadata_only"
        else:
            normalized["status"] = "candidate_found"
        normalized.setdefault(
            "blocker",
            "analysis-ready acquisition requires a staged data resource distinct "
            "from the discovery metadata catalog"
            if reused_metadata_as_data
            else "analysis-ready acquisition requires a staged local CSV path",
        )
    elif normalized.get("analysis_ready") is True and status == "staged" and local_path:
        normalized.pop("blocker", None)
    return normalized


def _merge_workflow_state_mapping(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Merge typed workflow state while preserving progressed semantic state."""

    for raw_key, raw_value in incoming.items():
        key = str(raw_key)
        if key == "provenance" and isinstance(raw_value, Mapping):
            provenance_state = {
                str(provenance_key): provenance_value
                for provenance_key, provenance_value in raw_value.items()
                if str(provenance_key) != "provenance" and isinstance(provenance_value, Mapping)
            }
            if provenance_state:
                _merge_workflow_state_mapping(target, provenance_state)
        if isinstance(raw_value, Mapping):
            incoming_value = _normalize_workflow_state_section(key, raw_value)
            current = target.get(key)
            if isinstance(current, Mapping):
                incoming_rank = _workflow_status_rank(key, incoming_value)
                current_rank = _workflow_status_rank(key, current)
                if incoming_rank < current_rank:
                    continue
                merged = dict(current)
                if (
                    key == "resource_candidate"
                    and current.get("geographically_grounded") is True
                    and incoming_value.get("geographically_grounded") is False
                ):
                    incoming_value = dict(incoming_value)
                    incoming_value.pop("geographically_grounded", None)
                _merge_non_empty_mapping(merged, incoming_value)
                target[key] = merged
            else:
                target[key] = incoming_value
        else:
            target[key] = raw_value


_TRAJECTORY_TOOL_NAME_KEYS = ("tool_name", "tool", "name")
_TRAJECTORY_TOOL_ARGS_KEYS = ("tool_args", "args", "arguments", "params")
_TRAJECTORY_TOOL_RESULT_KEYS = (
    "observation",
    "result",
    "output",
    "response",
    "tool_result",
    "tool_output",
)


def _trajectory_key_index(key: str, prefixes: tuple[str, ...]) -> str | None:
    normalized = key.strip().lower()
    for prefix in prefixes:
        if normalized == prefix:
            return ""
        match = re.fullmatch(rf"(?:step_)?(?P<idx>\d+)_{re.escape(prefix)}", normalized)
        if match:
            return str(match.group("idx"))
        match = re.fullmatch(rf"{re.escape(prefix)}_(?P<idx>\d+)", normalized)
        if match:
            return str(match.group("idx"))
    return None
