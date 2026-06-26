"""Evidence-grounding + tool-result helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that *grounds a turn's output in verifiable reality* and
*recovers bounded tool evidence* from agent trajectories. It is the single source
of truth for:

* **Artifact grounding** -- replacing fabricated local csv/png path citations in a
  final answer with the run's verified on-disk artifact of the same type, driven
  only by the typed ``workflow_state`` and the filesystem (no station/region
  heuristics): :func:`_ground_fabricated_local_artifact_paths` and its support
  (:func:`_verified_local_artifact_paths_by_ext`, :func:`_is_remote_artifact_ref`).
* **Tool-result inspection** -- previewing, error-classifying, and idempotently
  bounding individual tool results (:func:`_tool_result_preview`,
  :func:`_tool_result_is_error`, :func:`_is_bounded_tool_result`,
  :func:`_bounded_tool_call_result`).
* **Trajectory evidence recovery** -- pulling bounded tool-call evidence out of
  DSPy ReAct trajectories (:func:`_extract_tools_called_from_trajectory`), the
  no-prose-answer fallback that surfaces retained tool observations
  (:func:`_tool_agent_empty_answer_fallback`), and promotion of ``fs_propose_edit``
  tool results into file-diff proposals (:func:`_propose_edit_diffs_from_pred`).
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
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.workflow_state.merge import (
    _TRAJECTORY_TOOL_ARGS_KEYS,
    _TRAJECTORY_TOOL_NAME_KEYS,
    _TRAJECTORY_TOOL_RESULT_KEYS,
    _trajectory_key_index,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef


# ------------------------------------------------------------------------- #
# Artifact grounding #
# ------------------------------------------------------------------------- #


_ARTIFACT_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./~+-]+\.(?:csv|png)", re.IGNORECASE)
_ARTIFACT_PATH_MISSING_FRAMING_RE = re.compile(
    r"(not\s+(?:been\s+)?(?:staged|downloaded|available|present|found|created|generated|produced)|"
    r"no\s+(?:png|csv|plot|figure|file|artifact|local)\b|"
    r"does\s+not\s+exist|doesn'?t\s+exist|not\s+yet|is\s+blocked|blocked\s+because|"
    r"cannot\s+be|could\s+not\s+be|no\s+such\s+file|would\s+(?:need|be)|will\s+be|"
    r"written\s+to|saved\s+to|expected\s+(?:location|at)|placeholder|hypothetical|"
    r"once\s+(?:the|a)\b|to\s+be\s+(?:created|generated|written))",
    re.IGNORECASE,
)


def _is_remote_artifact_ref(value: str) -> bool:
    """Whether a path string is a remote/URL reference (never a local artifact)."""

    value = str(value or "")
    return value.startswith(("http://", "https://", "ftp://", "//")) or "://" in value


_VERIFIED_ARTIFACT_STATE_PATHS: tuple[tuple[str, ...], ...] = (
    # The analysis-ready staged station time-series CSV (never the metadata
    # catalog, which is recorded separately under acquisition.metadata_path).
    ("acquisition", "local_path"),
    # The rendered plot PNG.
    ("artifact", "path"),
    ("visualization", "path"),
    ("visualization", "plot_path"),
    ("visualization", "staged_plot_png"),
    # The profiled station CSV (same file as acquisition.local_path).
    ("profile", "path"),
)


def _verified_local_artifact_paths_by_ext(
    state: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Collect the run's authoritative on-disk artifact paths from the specific
    typed workflow_state fields that name a produced deliverable (the staged
    station CSV and the rendered PNG), bucketed by lowercase extension.

    Only these declared fields are consulted — not an arbitrary walk — so that
    incidental on-disk files such as the staged metadata catalog
    (``acquisition.metadata_path``) never count as the deliverable artifact and
    never make the substitution ambiguous. These are the only artifact paths a
    final answer may legitimately cite; any other local csv/png path it presents
    as a produced artifact is a model confabulation."""

    found: dict[str, list[str]] = {"csv": [], "png": []}
    for section, key in _VERIFIED_ARTIFACT_STATE_PATHS:
        section_obj = state.get(section)
        if not isinstance(section_obj, Mapping):
            continue
        token = section_obj.get(key)
        if not isinstance(token, str):
            continue
        token = token.strip()
        if not token or _is_remote_artifact_ref(token):
            continue
        lowered = token.lower()
        for ext in ("csv", "png"):
            if lowered.endswith("." + ext):
                try:
                    on_disk = Path(token).is_file()
                except OSError:
                    on_disk = False
                if on_disk and token not in found[ext]:
                    found[ext].append(token)
    return found


def _ground_fabricated_local_artifact_paths(
    answer: str,
    state: Mapping[str, Any],
) -> str:
    """Replace fabricated local artifact (csv/png) path citations in a final
    answer with the run's verified on-disk artifact of the same type.

    The synthesis model sometimes derives a plausible-but-wrong local artifact
    filename (e.g. an invented ``.../plots/<station>_timeseries.png`` or a
    ``<csv>.png`` swap) instead of copying the exact tool-returned path, and on a
    data-blocked run it can cite a local csv/png that was never produced at all.
    Such a path does not exist on disk and misrepresents the deliverable. This
    generic pass — driven only by the typed workflow_state and the filesystem,
    with no station/region heuristics — corrects a non-existent local csv/png
    citation: it rewrites it to the single verified artifact of that type when
    exactly one exists, otherwise (nothing real to point at, e.g. a data-blocked
    run) it neutralizes the fabricated path with an explicit not-produced note.
    Remote source URLs and paths the answer honestly frames as
    missing/not-yet-created are left untouched."""

    if not answer:
        return answer
    verified = _verified_local_artifact_paths_by_ext(state)

    result = answer
    for match in list(_ARTIFACT_PATH_TOKEN_RE.finditer(answer)):
        token = match.group(0)
        if _is_remote_artifact_ref(token):
            continue
        try:
            if Path(token).is_file():
                continue
        except OSError:
            continue
        ext = token.rsplit(".", 1)[-1].lower()
        candidates = verified.get(ext) or []
        # Path-doubling / prefix-mangling: if the non-existent token EMBEDS exactly
        # one verified artifact path as a substring (e.g. the model emitted
        # ".../ndp-/home/.../ndp-staging/P473.csv" — a real path with a duplicated
        # prefix), collapse to that verified path. Generic; runs before the
        # ambiguity check so it still corrects when several artifacts exist.
        embedded = [c for c in candidates if c and c in token and c != token]
        if len(embedded) == 1:
            result = result.replace(token, embedded[0])
            continue
        if len(candidates) > 1:
            # Ambiguous which verified artifact was meant; leave text unchanged.
            continue
        # Respect honest "not produced / would be at <path>" framing.
        lo = max(0, match.start() - 160)
        hi = min(len(answer), match.end() + 160)
        if _ARTIFACT_PATH_MISSING_FRAMING_RE.search(answer[lo:hi]):
            continue
        if len(candidates) == 1:
            # Exactly one verified artifact of this type: correct the citation.
            result = result.replace(token, candidates[0])
        else:
            # No real local artifact of this type exists this run: drop the
            # fabricated path rather than present an unproduced file as real.
            result = result.replace(token, f"[no local {ext} artifact was produced this run]")
    return result


# ------------------------------------------------------------------------- #
# Tool-result inspection #
# ------------------------------------------------------------------------- #


def _is_empty_dynamic_agent_answer_error(exc: Exception) -> bool:
    """Return whether a dynamic expert failed only because it produced no answer."""

    return "returned an empty answer" in str(exc)


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


def _bounded_tool_call_result(value: Any, *, max_result_chars: int = 12000) -> Any:
    """Return a JSON-safe bounded result payload for assistant metadata."""

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


# ------------------------------------------------------------------------- #
# Trajectory evidence recovery #
# ------------------------------------------------------------------------- #


_TOOL_TRAJECTORY_EVIDENCE_KEYS = (
    "observation",
    "observations",
    "result",
    "results",
    "output",
    "outputs",
    "response",
    "responses",
    "tool_result",
    "tool_results",
    "tool_output",
    "tool_outputs",
)


def _tool_agent_empty_answer_fallback(trajectory: Any, *, max_items: int = 6) -> str:
    """Return bounded tool evidence when a ReAct tool agent produced no answer."""

    if not trajectory:
        return ""

    evidence: list[tuple[str, Any]] = []

    def collect(label: str, value: Any) -> None:
        if len(evidence) >= max_items:
            return
        if _tool_result_is_error(value):
            return
        preview = _tool_result_preview(value).strip()
        normalized_preview = preview.rstrip(".").casefold()
        if not preview or normalized_preview == "completed":
            return
        evidence.append((label, value))

    def visit(label: str, value: Any) -> None:
        if len(evidence) >= max_items:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_label = f"{label}.{key}" if label else str(key)
                normalized_key = str(key).lower()
                if any(token in normalized_key for token in _TOOL_TRAJECTORY_EVIDENCE_KEYS):
                    collect(child_label, child)
                elif isinstance(child, Mapping | list | tuple):
                    visit(child_label, child)
            return
        if isinstance(value, list | tuple):
            for idx, item in enumerate(value):
                visit(f"{label}[{idx}]" if label else f"[{idx}]", item)

    visit("trajectory", trajectory)
    if not evidence:
        return ""

    lines = [
        "The tool-backed expert produced no final prose answer, but CLIO retained "
        "successful tool-grounded evidence from its ReAct trajectory.",
        "",
        "Retained tool observations:",
    ]
    for label, value in evidence:
        preview = _tool_result_preview(value).strip()
        if len(preview) > 1200:
            preview = f"{preview[:1200].rstrip()}..."
        lines.append(f"- {label}: {preview}")
    return "\n".join(lines)


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
            "provider_source": ("agent_default" if agent_def.default_provider else "global_active"),
            "model_source": "agent_default" if agent_def.default_model else "global_active",
            "fallback_to_global": not (agent_def.default_provider and agent_def.default_model),
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
    if agent_def.source == "expert_pack":
        payload.update(
            {
                "parent_id": agent_def.parent_id,
                "skills": list(agent_def.skills),
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
