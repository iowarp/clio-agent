"""Pure workflow_state merge/normalize helpers (#714).

Behavior-preserving extraction from ``clio_agent.gact.app``. These helpers are
pure stdlib (``re``, ``pathlib.Path``, ``collections.abc.Mapping``) and call
only each other; they read no contextvars and no module-level app state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


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


def _merge_workflow_state_mapping(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
    *,
    schema: "WorkflowStateSchema",
) -> None:
    """Merge typed workflow state while preserving progressed semantic state.

    Precedence, normalization, and sticky-field rules are all declared by the
    pack ``schema`` (rank / normalize_section / sticky_true_fields_for); this
    function contributes only the generic merge mechanics (provenance flattening,
    higher-rank-wins, non-empty overwrite).
    """

    for raw_key, raw_value in incoming.items():
        key = str(raw_key)
        if key == "provenance" and isinstance(raw_value, Mapping):
            provenance_state = {
                str(provenance_key): provenance_value
                for provenance_key, provenance_value in raw_value.items()
                if str(provenance_key) != "provenance" and isinstance(provenance_value, Mapping)
            }
            if provenance_state:
                _merge_workflow_state_mapping(target, provenance_state, schema=schema)
        if isinstance(raw_value, Mapping):
            incoming_value = schema.normalize_section(key, raw_value)
            current = target.get(key)
            if isinstance(current, Mapping):
                incoming_rank = schema.rank(key, incoming_value)
                current_rank = schema.rank(key, current)
                if incoming_rank < current_rank:
                    continue
                merged = dict(current)
                stripped_incoming = False
                for sticky_field in schema.sticky_true_fields_for(key):
                    if current.get(sticky_field) is True and incoming_value.get(sticky_field) is False:
                        if not stripped_incoming:
                            incoming_value = dict(incoming_value)
                            stripped_incoming = True
                        incoming_value.pop(sticky_field, None)
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
