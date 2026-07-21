"""Pure workflow_state merge/normalize helpers (#714).

Behavior-preserving extraction from ``clio_agent.gact.app``. These helpers are
pure stdlib (``re``, ``pathlib.Path``, ``collections.abc.Mapping``) and call
only each other; they read no contextvars and no module-level app state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


# ---------------------------------------------------------------------------
# Deterministic request-order ensemble merge (#948 S5).
#
# When several concurrently-spawned children (an ensemble, or a fan-out) return
# typed ``workflow_state`` sections that COLLIDE on a top-level key, the winner MUST
# be chosen by REQUEST ORDER (spawn order = ``run_index``), never by completion
# order — completion order is nondeterministic (the fastest child finishes first),
# so a completion-order last-writer would make the merged state depend on timing.
# Request order is deterministic: apply runs in ``run_index`` order and the highest
# index wins each key. Every collision on DIFFERENT values is surfaced as a typed
# ``workflow_state_merge_conflict`` row (winner run + loser runs) — no silent
# last-writer; the model sees the conflict and decides.
# ---------------------------------------------------------------------------

MERGE_CONFLICT_REASON = "workflow_state_merge_conflict"


@dataclass(frozen=True)
class RunWorkflowState:
    """One ensemble run's contribution to the merge: its durable ``run_index``, its
    ``task_id`` and ``agent_id`` (for attribution in a conflict row) and its typed
    ``workflow_state``. ``agent_id`` is the child expert id — required because a
    heterogeneous fan-out can produce several runs sharing ``run_index==0`` (each is the
    first of its OWN expert), so ``run_index`` alone does not identify the run (#953 [1])."""

    run_index: int
    task_id: str
    workflow_state: Mapping[str, Any]
    agent_id: str = ""


def _canonical(value: Any) -> str:
    """A stable, order-insensitive canonical form for equality of two run values.

    JSON with sorted keys so ``{"a": 1, "b": 2}`` == ``{"b": 2, "a": 1}``; falls back
    to ``repr`` for anything not JSON-serializable (never raises — an unserializable
    value must not crash a merge)."""

    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr makes this rare
        return repr(value)


def merge_run_workflow_states(
    runs: Sequence[RunWorkflowState],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Merge ensemble runs' ``workflow_state`` in REQUEST ORDER; return
    ``(merged, conflicts)``.

    ``runs`` may arrive in ANY order (e.g. completion order); the merge sorts by
    ``run_index`` FIRST, so the result is independent of arrival/completion order —
    that determinism is the whole point (a completion-order merge is the bug this
    replaces). Per top-level key, the highest ``run_index`` that set it wins. A key
    two-or-more runs set to DIFFERENT values yields one conflict row::

        {"reason": "workflow_state_merge_conflict", "key": <key>,
         "winner": {"run_index": int, "task_id": str, "agent_id": str},
         "loser_runs": [{"run_index": int, "task_id": str, "agent_id": str}, ...]}

    ``loser_runs`` lists (in request order) every earlier run whose value differs
    from the winner's. A key all runs agree on is merged with no conflict row.

    Cross-expert tie-break (#953 [1]): ``run_index`` is assigned PER child expert, so a
    heterogeneous fan-out (e.g. ``researcher`` + ``analyst``) yields several runs at the
    SAME ``run_index`` (each first-of-its-own-expert). The sort is STABLE, so ties break by
    the caller's wait-list order (the model's ``task_ids`` argument order). ``agent_id`` on
    each attribution dict disambiguates such same-index runs — never rely on ``run_index``
    alone to identify a run across a heterogeneous batch.
    """

    ordered = sorted(runs, key=lambda run: run.run_index)
    per_key: dict[str, list[RunWorkflowState]] = {}
    order: list[str] = []
    for run in ordered:
        state = run.workflow_state or {}
        if not isinstance(state, Mapping):
            continue
        for raw_key in state:
            key = str(raw_key)
            if key not in per_key:
                per_key[key] = []
                order.append(key)
            per_key[key].append(run)

    merged: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for key in order:
        contributors = per_key[key]
        winner = contributors[-1]  # highest run_index (request order last-wins)
        winner_value = winner.workflow_state[key]
        merged[key] = winner_value
        winner_canonical = _canonical(winner_value)
        losers = [
            {"run_index": run.run_index, "task_id": run.task_id, "agent_id": run.agent_id}
            for run in contributors[:-1]
            if _canonical(run.workflow_state[key]) != winner_canonical
        ]
        if losers:
            conflicts.append(
                {
                    "reason": MERGE_CONFLICT_REASON,
                    "key": key,
                    "winner": {
                        "run_index": winner.run_index,
                        "task_id": winner.task_id,
                        "agent_id": winner.agent_id,
                    },
                    "loser_runs": losers,
                }
            )
    return merged, conflicts


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
                    if (
                        current.get(sticky_field) is True
                        and incoming_value.get(sticky_field) is False
                    ):
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
