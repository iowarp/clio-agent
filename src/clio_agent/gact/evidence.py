"""Evidence-grounding + tool-result helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that *grounds a turn's output in verifiable reality* and
*recovers bounded tool evidence* from agent trajectories. It is the single source
of truth for:

* **Tool-result inspection** -- previewing, error-classifying, and idempotently
  bounding individual tool results (:func:`_tool_result_preview`,
  :func:`_tool_result_is_error`, :func:`_is_bounded_tool_result`,
  :func:`_bounded_tool_call_result`).
* **Trajectory evidence projection** -- pulling bounded tool-call evidence out of
  DSPy ReAct trajectories (:func:`_extract_tools_called_from_trajectory`) and
  promoting ``fs_propose_edit`` tool results into file-diff proposals
  (:func:`_propose_edit_diffs_from_pred`).
* **Runtime provenance** -- non-secret provenance for the dynamic agent used this
  turn (:func:`_dynamic_agent_runtime_provenance`).

The module imports only leaves: stdlib plus the pure trajectory-key primitives in
:mod:`clio_agent.gact.workflow_state.merge`. ``_active_lm_model_ref`` (the active
LM reference reader in :mod:`clio_agent.gact.providers.config`) is imported lazily
inside :func:`_dynamic_agent_runtime_provenance` to keep this module free of any
provider-package import at module top. It never imports :mod:`clio_agent.gact.app`.

Note: the evidence-index helper (``_compact_exact_evidence_index``) lives with its
tightly coupled callers in :mod:`clio_agent.gact.delegation`; it is deliberately not
duplicated here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.workflow_state.merge import (
    _TRAJECTORY_TOOL_ARGS_KEYS,
    _TRAJECTORY_TOOL_NAME_KEYS,
    _TRAJECTORY_TOOL_RESULT_KEYS,
    _trajectory_key_index,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef

logger = logging.getLogger(__name__)
TOOL_RESULT_TRUNCATED_REASON = "tool_result_oversize"


# ------------------------------------------------------------------------- #
# Tool-result inspection #
# ------------------------------------------------------------------------- #


def _tool_result_preview(result: Any) -> str:
    """Render a tool result as a compact, JSON-stable preview string."""

    if result is None:
        return "completed"
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def _tool_result_is_error(result: Any) -> bool:
    """Return whether a tool result represents an error/failed outcome."""

    if isinstance(result, Mapping):
        if result.get("error"):
            return True
        status = str(result.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            return True
        ok = result.get("ok")
        if ok is False:
            return True
    elif isinstance(result, str):
        normalized = result.strip().casefold()
        if normalized.startswith("{") and '"error"' in normalized:
            return True
        if any(
            token in normalized
            for token in (
                "file_not_found",
                "file does not exist",
                "tool_error",
                "status=error",
                "status=failed",
            )
        ):
            return True
    return False


def _is_bounded_tool_result(value: Any) -> bool:
    """True if ``value`` is already a bounded-preview payload.

    Bounding must be IDEMPOTENT: a bounded result
    (``{"preview": ..., "truncated": True, "original_chars": N}``) flows across
    stages (tool -> catalog -> data -> main) and was being re-bounded at each hop,
    nesting preview-of-preview-of-preview (observed: a geo_filter result wrapped
    22x by turn.completed, burying the real data so the staged station could not be
    verified in-region). Detecting an already-bounded payload here stops the nesting.
    """
    return (
        isinstance(value, Mapping)
        and value.get("truncated") is True
        and "preview" in value
        and "original_chars" in value
    )


def _bounded_tool_call_result(value: Any, *, max_result_chars: int | None = None) -> Any:
    """Return a JSON-safe bounded result payload for assistant metadata."""

    if _is_bounded_tool_result(value):
        return value  # already bounded -> never re-wrap (idempotent)
    if max_result_chars is None:
        max_result_chars = conf.resolve(
            "limits.tool_result_chars",
            env="CLIO_TOOL_RESULT_CHARS",
            default=12_000,
            cast=conf.as_int,
        )
    preview = _tool_result_preview(value)
    if len(preview) <= max_result_chars:
        return value
    logger.info(
        "tool result bounded reason=%s original_chars=%d preview_chars=%d",
        TOOL_RESULT_TRUNCATED_REASON,
        len(preview),
        max_result_chars,
    )
    return {
        "preview": preview[:max_result_chars].rstrip(),
        "truncated": True,
        "original_chars": len(preview),
    }


# ------------------------------------------------------------------------- #
# Trajectory evidence projection #
# ------------------------------------------------------------------------- #


def _extract_tools_called_from_trajectory(
    trajectory: Any,
    *,
    max_items: int = 32,
    max_result_chars: int = 12000,
) -> list[dict[str, Any]]:
    """Recover bounded tool-call evidence from DSPy ReAct trajectories.

    DSPy versions and adapters vary in trajectory shape. This intentionally
    accepts the common indexed mapping form (`tool_name_0`, `tool_args_0`,
    `observation_0`) and nested/list step forms while preserving enough result
    evidence for post-run scientific audit.
    """

    rows: list[dict[str, Any]] = []

    def bounded_result(value: Any) -> Any:
        if _is_bounded_tool_result(value):
            return value  # already bounded -> never re-wrap (idempotent)
        preview = _tool_result_preview(value)
        if len(preview) <= max_result_chars:
            return value
        return {
            "preview": preview[:max_result_chars].rstrip(),
            "truncated": True,
            "original_chars": len(preview),
        }

    def append_row(row: Mapping[str, Any]) -> None:
        if len(rows) >= max_items:
            return
        name = str(row.get("name") or row.get("tool") or "").strip()
        result = row.get("result")
        args = row.get("args")
        if not name and result is None:
            return
        out: dict[str, Any] = {}
        if name:
            out["name"] = name
        if args is not None:
            out["args"] = args
        if result is not None:
            out["result"] = bounded_result(result)
            out["ok"] = not _tool_result_is_error(result)
        out.setdefault("telemetry_source", "agent_trajectory")
        rows.append(out)

    def visit(value: Any) -> None:
        if len(rows) >= max_items:
            return
        if isinstance(value, Mapping):
            # Direct step row: {"tool_name": ..., "tool_args": ..., "observation": ...}
            direct: dict[str, Any] = {}
            for key in _TRAJECTORY_TOOL_NAME_KEYS:
                if key in value:
                    direct["name"] = value[key]
                    break
            for key in _TRAJECTORY_TOOL_ARGS_KEYS:
                if key in value:
                    direct["args"] = value[key]
                    break
            for key in _TRAJECTORY_TOOL_RESULT_KEYS:
                if key in value:
                    direct["result"] = value[key]
                    break
            if direct:
                append_row(direct)
                for raw_key, child in value.items():
                    normalized_key = str(raw_key).lower()
                    if (
                        _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_NAME_KEYS)
                        is not None
                        or _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_ARGS_KEYS)
                        is not None
                        or _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_RESULT_KEYS)
                        is not None
                    ):
                        continue
                    if isinstance(child, Mapping | list | tuple):
                        visit(child)
                return

            # Indexed flat row: {"step_0_tool_name": ..., "step_0_observation": ...}
            indexed: dict[str, dict[str, Any]] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                name_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_NAME_KEYS)
                if name_index is not None:
                    indexed.setdefault(name_index, {})["name"] = child
                    continue
                args_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_ARGS_KEYS)
                if args_index is not None:
                    indexed.setdefault(args_index, {})["args"] = child
                    continue
                result_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_RESULT_KEYS)
                if result_index is not None:
                    indexed.setdefault(result_index, {})["result"] = child
                    continue
                if isinstance(child, Mapping | list | tuple):
                    visit(child)
            for index in sorted(indexed, key=lambda item: int(item) if item.isdigit() else -1):
                append_row(indexed[index])
            return
        if isinstance(value, list | tuple):
            for child in value:
                visit(child)

    visit(trajectory)
    return rows


def _propose_edit_diffs_from_pred(pred: Any) -> list[dict[str, Any]]:
    """Promote successful ``fs_propose_edit`` tool results into file-diff proposals.

    A dynamic tool agent calls ``fs_propose_edit`` as a TOOL; unlike the builtin
    edit experts it does not populate ``pred.file_diffs``, so the returned
    proposal (path + unified_diff + new_content) never became a ``file_diff``
    part or a pending ``/v1/sessions/{sid}/diffs`` row — the TUI could see the
    tool call but never the diff (iowarp/clio-agent#674). Recover the proposals
    from the turn's tool results so the standard materialization picks them up.

    Reads ``pred.tools_called`` (which already carries each call's structured
    result), falling back to parsing ``pred.trajectory``. Only successful
    (``ok``) calls whose result carries a ``path`` and a diff/new_content are
    promoted; duplicates by (path, diff-prefix) are collapsed.
    """

    rows: list[Any] = list(getattr(pred, "tools_called", None) or [])
    if not rows:
        rows = _extract_tools_called_from_trajectory(getattr(pred, "trajectory", None))
    diffs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or row.get("tool") or "")
        if "propose_edit" not in name:
            continue
        if row.get("ok") is False:
            continue
        result = row.get("result")
        if not isinstance(result, Mapping):
            continue
        path = str(result.get("path") or "").strip()
        unified_diff = result.get("unified_diff") or ""
        new_content = result.get("new_content")
        if not path or (not unified_diff and new_content is None):
            continue
        key = (path, str(unified_diff)[:64])
        if key in seen:
            continue
        seen.add(key)
        diff: dict[str, Any] = {"path": path, "unified_diff": unified_diff}
        if new_content is not None:
            diff["new_content"] = new_content
        for extra in ("edit_mode", "lines_added", "lines_removed"):
            if extra in result:
                diff[extra] = result[extra]
        diffs.append(diff)
    return diffs


# ------------------------------------------------------------------------- #
# Runtime provenance #
# ------------------------------------------------------------------------- #


def _dynamic_agent_runtime_provenance(
    app: "FastAPI",
    agent_def: "AgentDef",
    *,
    execution_mode: str,
) -> dict[str, Any]:
    """Return non-secret provenance for the dynamic agent used this turn."""

    from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

    active_model = _active_lm_model_ref(app)
    provider_id = agent_def.default_provider or active_model.get("provider_id", "")
    model_id = agent_def.default_model or active_model.get("model_id", "")
    # A per-message/session model route accepted by ``message_submission`` is
    # overlaid onto this turn's local agent copy (turn_forward
    # ._apply_turn_model_selection) and stamps its own source, so the provenance
    # names the layer that actually chose the model rather than reporting the
    # overlay as the agent's own default.
    turn_model_source = str(agent_def.metadata.get("turn_model_selection_source") or "")
    provider_source = turn_model_source or (
        "agent_default" if agent_def.default_provider else "global_active"
    )
    model_source = turn_model_source or (
        "agent_default" if agent_def.default_model else "global_active"
    )
    payload: dict[str, Any] = {
        "kind": "dynamic_agent",
        "agent_id": agent_def.id,
        "source": agent_def.source,
        "title": agent_def.title,
        "execution_mode": execution_mode,
        "module": dict(agent_def.module),
        "tools": list(agent_def.tools),
        "structured_outputs": dict(agent_def.structured_outputs),
        "fanout": dict(agent_def.fanout),
        "prompt": {
            "source": "agent_definition",
            "has_system_prompt": bool(agent_def.system_prompt.strip()),
        },
        "model": {
            "provider_id": provider_id,
            "model_id": model_id,
            "provider_source": provider_source,
            "model_source": model_source,
            "fallback_to_global": not turn_model_source
            and not (agent_def.default_provider and agent_def.default_model),
        },
    }
    blueprint_id = str(agent_def.metadata.get("agent_blueprint_id") or "").strip()
    if blueprint_id:
        payload["agent_blueprint"] = {
            "id": blueprint_id,
            "version": str(agent_def.metadata.get("agent_blueprint_version") or ""),
            "scope": str(agent_def.metadata.get("agent_blueprint_scope") or ""),
            "definition_path": str(agent_def.metadata.get("agent_blueprint_definition_path") or ""),
        }
    overlay = agent_def.metadata.get("agent_blueprint_overlay")
    if isinstance(overlay, Mapping):
        payload["agent_overlay"] = dict(overlay)
        fields = (
            set(overlay.get("fields") or []) if isinstance(overlay.get("fields"), list) else set()
        )
        if "system_prompt" in fields:
            payload["prompt"]["source"] = "session_agent_overlay"
    # resolved_skills (#920): the RUNTIME-truth resolution the executing expert
    # actually had (from the per-app build cache) — source-agnostic, and the
    # only record that includes default-root workspace auto-declarations.
    from clio_agent.gact.runtime.app_state import per_app_dict  # noqa: PLC0415

    skill_rt = per_app_dict("skill_runtime_cache", app=app).get(agent_def.id)
    resolutions = getattr(skill_rt, "resolutions", None)
    if resolutions:
        payload["resolved_skills"] = {
            skill_id: res.to_metadata() for skill_id, res in resolutions.items()
        }
    if agent_def.source == "expert_pack":
        payload.update(
            {
                "parent_id": agent_def.parent_id,
                "skills": list(agent_def.skills),
                # Typed per-id resolution outcome (#920): resolved/missing/
                # ambiguous/unreadable + path/scope/checksum, from row load (S1).
                "skill_resolution": dict(agent_def.metadata.get("skill_resolution") or {}),
                "commands": list(agent_def.commands),
                "pack": {
                    "id": str(agent_def.metadata.get("pack_id") or ""),
                    "version": str(agent_def.metadata.get("pack_version") or ""),
                    "scope": str(
                        agent_def.metadata.get("pack_scope")
                        or agent_def.metadata.get("expert_scope")
                        or ""
                    ),
                    "definition_path": str(
                        agent_def.metadata.get("definition_path")
                        or agent_def.metadata.get("pack_definition_path")
                        or agent_def.metadata.get("expert_path")
                        or ""
                    ),
                },
            }
        )
    return payload
