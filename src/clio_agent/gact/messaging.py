"""Message / multimodal + ask-user helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that shapes a turn's *message I/O* — building transcript
parts, bridging image parts to native-vision model inputs, coercing/rendering
ask-user planner actions, and summarizing predictions/subagent inputs for the
trace. It is the single source of truth for:

* **Multimodal message parts** — whether an agent's ``forward`` accepts native
  inputs, assembling transcript parts for a user
  turn while preserving image parts (:func:`_user_message_parts`), bounded
  image-part metadata for logging without raw base64
  (:func:`_image_part_summaries`), and converting GACT image parts to DSPy image
  inputs (:func:`_dspy_images_from_parts`, :func:`_dspy_files_from_parts`).
* **Ask-user planner actions** — extracting an ask-user action from a prediction
  (:func:`_coerce_ask_user_action`), turning it into typed question options
  (:func:`_ask_user_options_from_action`), and rendering an answered question
  back into resume text (:func:`_ask_user_resume_text`).
* **Trace summaries** — the routing/predict trace payload for a prediction
  (:func:`_prediction_summary`) and a human-readable rendering of a materialized
  subagent input (:func:`_format_subagent_input`).

The module imports stdlib, the runtime id primitive, the shared wire coercer,
and the GACT message/question types. ``dspy`` is imported lazily inside
:func:`_dspy_images_from_parts` to keep this module free of any heavy engine
import at module top. It never imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import base64
import inspect
import json
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.hooks.defer import HOOK_DEFER_RESUME_META
from clio_agent.gact.runtime.globals import _new_part_id
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Part,
    UserQuestion,
    UserQuestionOption,
)
from clio_agent.tools.mcp_runtime import wire_value

# ------------------------------------------------------------------------- #
# Reserved client-metadata keys (#1057 B2)                                   #
# ------------------------------------------------------------------------- #

# Internal turn-control metadata keys that server-side producers stamp on a
# staged/resumed/steered message to drive privileged turn behaviour (skip a
# just-approved hook, redrive a deferred stop, resume a plan-exit, mark a turn
# synthetic/scheduled, ...). A client message body must NEVER be allowed to carry
# any of these: a smuggled ``hook_defer_resume`` bypasses the UserPromptSubmit
# hook outright (the B2 blocker), and the rest are equivalent privilege-escalation
# vectors. The POST /messages ingest rejects (never strips — stripping is silent
# coercion) any client metadata intersecting this set with a typed 400.
#
# Each literal names the OWNER module that legitimately produces it; the one
# defined owner constant (``HOOK_DEFER_RESUME_META``) is imported rather than
# duplicated. Keys marked "P4" are forward-compat reservations for the loop/goal
# merge (feat/p4-loop-goal-cron) so a client can never race the feature in.
RESERVED_CLIENT_METADATA_KEYS: frozenset[str] = frozenset(
    {
        HOOK_DEFER_RESUME_META,  # hooks/defer.py — UserPromptSubmit resume once-gate
        "retry_attempt_id",  # routes/sessions.py — retry staging
        "ask_user_resume",  # routes/sessions.py — ask-user answer fold
        "question_id",  # plan_mode.py / hooks/defer.py — pending-question join key
        "plan_exit_resume",  # plan_mode.py — plan-exit approval resume
        "plan_exit_result",  # plan_mode.py — plan-exit outcome
        "plan_exit_mode",  # plan_mode.py — approved plan-exit mode
        "plan_exit_context_cleared",  # plan_mode.py — plan-exit context-clear marker
        "stop_defer_redrive",  # hooks/defer.py — Stop-defer redrive
        "goal_redrive",  # goal.py (P4) — goal redrive
        "goal_id",  # goal.py (P4)
        "goal_iters",  # goal.py (P4)
        "goal_reason",  # goal.py (P4)
        "mid_turn_steer",  # loop_inbox.py — mid-turn steer marker
        "scheduled",  # app.py — scheduler-fired turn marker
        "schedule_id",  # app.py — scheduler-fired turn id
        "synthetic",  # compaction/catalog — server-synthesized message marker
    }
)


def reserved_metadata_keys(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return the sorted reserved control keys a client metadata mapping carries.

    Args:
        metadata: The client-supplied per-message metadata (may be ``None``).

    Returns:
        The sorted list of keys in ``metadata`` that intersect
        :data:`RESERVED_CLIENT_METADATA_KEYS`; empty when the mapping is benign.
    """

    if not metadata:
        return []
    return sorted(RESERVED_CLIENT_METADATA_KEYS.intersection(metadata))


def raise_on_reserved_metadata(session_id: str, metadata: Mapping[str, Any] | None) -> None:
    """Reject client metadata that carries a reserved internal turn-control key.

    The single typed-rejection chokepoint shared by every client-writable message
    ingest (POST ``/messages`` and the ``/retry`` sibling): a smuggled
    ``hook_defer_resume`` (or any :data:`RESERVED_CLIENT_METADATA_KEYS` member) would
    ride the client mapping onto the staged ``user_msg.metadata`` the UserPromptSubmit
    hook reads, bypassing governance (the B2 blocker). The mapping is rejected, never
    stripped — stripping is silent coercion.

    Args:
        session_id: The session the ingest targets; echoed in the error detail.
        metadata: The client-supplied per-message metadata (may be ``None``).

    Raises:
        HTTPException: 400 ``reserved_metadata_key`` naming the offending keys when
            ``metadata`` intersects :data:`RESERVED_CLIENT_METADATA_KEYS`.
    """

    offending = reserved_metadata_keys(metadata)
    if not offending:
        return
    raise HTTPException(
        status_code=400,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="reserved_metadata_key",
                message=(
                    "request metadata carried reserved internal control "
                    f"key(s): {', '.join(offending)}"
                ),
                details={"session_id": session_id, "reserved_keys": offending},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


# ------------------------------------------------------------------------- #
# Trace summaries                                                            #
# ------------------------------------------------------------------------- #


def _prediction_summary(pred: Any) -> dict[str, Any]:
    summary = {
        "selected_expert": str(getattr(pred, "selected_expert", "") or ""),
        "route_source": str(getattr(pred, "route_source", "") or ""),
        "route_reason": str(
            getattr(pred, "route_reason", "") or getattr(pred, "routing_rationale", "") or ""
        ),
        "answer": str(getattr(pred, "answer", "") or ""),
        "expert_handoffs": wire_value(
            getattr(pred, "expert_handoffs", None) or [], mode="gact_runtime"
        ),
        "tools_called": wire_value(getattr(pred, "tools_called", None) or [], mode="gact_runtime"),
        "file_diffs": wire_value(getattr(pred, "file_diffs", None) or [], mode="gact_runtime"),
        "error_info": wire_value(getattr(pred, "error_info", None), mode="gact_runtime"),
    }
    # Full capture (durable trace): the dspy ReAct trajectory and the extract's
    # chain-of-thought reasoning. These are in SENSITIVE_KEYS, so the SSE
    # projection strips them while the canonical trace keeps them for debugging
    # and (later) re-extract repair. Only attach when present to keep the
    # routing/predict payloads lean.
    trajectory = getattr(pred, "trajectory", None)
    if trajectory:
        summary["trajectory"] = wire_value(trajectory, mode="gact_runtime")
    reasoning = getattr(pred, "reasoning", None)
    if reasoning:
        summary["reasoning"] = str(reasoning)
    return summary


def _format_subagent_input(spawn_input: Any) -> str:
    """Format a materialized nanoagent input without a raw Python-dict look."""

    if isinstance(spawn_input, str):
        return spawn_input
    try:
        return "Subagent input:\n" + json.dumps(spawn_input, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return f"Subagent input:\n{spawn_input}"


# ------------------------------------------------------------------------- #
# Ask-user planner actions                                                  #
# ------------------------------------------------------------------------- #


def _coerce_ask_user_action(pred: Any) -> dict[str, Any]:
    """Extract an ask-user planner action from a prediction-like object."""

    candidates = [
        getattr(pred, "ask_user", None),
        getattr(pred, "user_question", None),
        getattr(pred, "action", None),
    ]
    action_json = getattr(pred, "action_json", None)
    if isinstance(action_json, str) and action_json.strip():
        try:
            candidates.append(json.loads(action_json))
        except json.JSONDecodeError:
            pass
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if not isinstance(raw, Mapping):
            continue
        action = str(raw.get("action") or raw.get("type") or "").strip().lower()
        if action and action not in {"ask_user", "question", "user_question"}:
            continue
        question = str(raw.get("question") or raw.get("prompt") or "").strip()
        if not question:
            continue
        choices_raw = raw.get("choices") or raw.get("options") or []
        choices = choices_raw if isinstance(choices_raw, list) else []
        return {
            "question": question,
            "choices": [c for c in choices if isinstance(c, Mapping)],
            "allow_freeform": bool(raw.get("allow_freeform", True)),
            "kind": str(raw.get("kind") or "").strip(),
            "reason": str(raw.get("reason") or raw.get("category") or "").strip(),
            "caller": raw.get("caller") if isinstance(raw.get("caller"), Mapping) else {},
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {},
        }
    return {}


def _ask_user_options_from_action(action: Mapping[str, Any]) -> list[UserQuestionOption]:
    options: list[UserQuestionOption] = []
    for idx, choice in enumerate(action.get("choices", []) or []):
        if not isinstance(choice, Mapping):
            continue
        label = str(choice.get("label") or choice.get("title") or choice.get("id") or "").strip()
        value = str(choice.get("value") or choice.get("id") or label).strip()
        description = str(choice.get("description") or "").strip()
        if not label:
            continue
        options.append(
            UserQuestionOption(
                label=label,
                value=value or f"choice_{idx + 1}",
                description=description,
            )
        )
    return options


def _ask_user_resume_text(question: UserQuestion) -> str:
    selected = ", ".join(question.selected_options)
    answer = question.answer.strip()
    lines = [
        "[Answer to agent question]",
        f"Question: {question.prompt}",
    ]
    if selected:
        lines.append(f"Selected option(s): {selected}")
    if answer:
        lines.append(f"Answer: {answer}")
    return "\n".join(lines)


# ------------------------------------------------------------------------- #
# Multimodal message parts                                                  #
# ------------------------------------------------------------------------- #


def _agent_accepts_images(agent: Any) -> bool:
    """Return whether agent.forward can receive native image inputs."""

    forward = getattr(agent, "forward", None)
    if not callable(forward):
        return False
    try:
        params = inspect.signature(forward).parameters
    except (TypeError, ValueError):
        return False
    if "images" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _user_message_parts(
    *,
    request_parts: list[Part],
    user_text: str,
) -> list[Part]:
    """Return transcript parts for a user turn, preserving typed user parts."""

    if not request_parts:
        return [Part(id=_new_part_id(), type="text", text=user_text)]
    parts: list[Part] = []
    has_text = False
    for part in request_parts:
        if part.type not in {
            "text",
            "image",
            "artifact_review",
            "resource_ref",
            "context_ref",
        }:
            continue
        metadata = dict(part.metadata)
        if part.type == "image":
            metadata.setdefault("clio_multimodal", "preserved")
        copied = part.model_copy(
            update={
                "id": part.id or _new_part_id(),
                "metadata": metadata,
            }
        )
        if copied.type == "text" and copied.text:
            has_text = True
        parts.append(copied)
    if not has_text and user_text:
        parts.insert(0, Part(id=_new_part_id(), type="text", text=user_text))
    return parts or [Part(id=_new_part_id(), type="text", text=user_text)]


def _image_part_summaries(parts: list[Part]) -> list[dict[str, Any]]:
    """Return bounded metadata for image parts without logging raw base64."""

    rows: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if part.type != "image":
            continue
        rows.append(
            {
                "index": index,
                "id": part.id,
                "media_type": part.media_type or part.metadata.get("media_type", ""),
                "has_data": bool(part.data),
                "data_length": len(part.data or ""),
                "url": part.url,
                "metadata": {
                    key: value
                    for key, value in part.metadata.items()
                    if key not in {"data", "base64", "file"}
                },
            }
        )
    return rows


def _resource_ref_image(part: Part, *, app: Any | None, workspace_id: str) -> Any | None:
    """Return the DSPy image for a resource whose delivery plan chose ``native``."""

    delivery = part.metadata.get("delivery")
    representation = str(delivery.get("representation") or "") if isinstance(delivery, dict) else ""
    if representation != "native" or app is None or not workspace_id:
        return None
    record = app.state.resource_store.get(workspace_id, part.resource_id)
    if (
        record is None
        or str(record.revision) != str(part.resource_revision)
        or record.state != "ready"
        or not record.detected_mime.startswith("image/")
    ):
        return None
    import dspy  # noqa: PLC0415

    original = app.state.resource_store.content_path(record).read_bytes()
    encoded = base64.b64encode(original).decode("ascii")
    return dspy.Image(f"data:{record.detected_mime};base64,{encoded}")


def _resource_ref_file(part: Part, *, app: Any | None, workspace_id: str) -> Any | None:
    """Return a DSPy PDF for a resource whose delivery plan chose ``native``."""

    delivery = part.metadata.get("delivery")
    representation = str(delivery.get("representation") or "") if isinstance(delivery, dict) else ""
    if representation != "native" or app is None or not workspace_id:
        return None
    record = app.state.resource_store.get(workspace_id, part.resource_id)
    if (
        record is None
        or str(record.revision) != str(part.resource_revision)
        or record.state != "ready"
        or record.detected_mime != "application/pdf"
    ):
        return None
    import dspy  # noqa: PLC0415

    original = app.state.resource_store.content_path(record).read_bytes()
    return dspy.File.from_bytes(
        original,
        filename=record.name,
        mime_type=record.detected_mime,
    )


def _dspy_images_from_parts(
    parts: list[Part],
    *,
    app: Any | None = None,
    workspace_id: str = "",
) -> list[Any]:
    """Convert native-planned GACT image parts and resources to DSPy images.

    ``resource_ref`` is the normal upload contract. Its immutable original is
    eligible only when delivery planning explicitly selected ``native`` from a
    live provider handshake; text-only or unknown models therefore keep using
    bounded resource tools and never receive image bytes optimistically.
    """

    images: list[Any] = []
    for part in parts:
        if part.type == "resource_ref":
            resource_image = _resource_ref_image(part, app=app, workspace_id=workspace_id)
            if resource_image is not None:
                images.append(resource_image)
            continue
        if part.type != "image":
            continue
        try:
            import dspy  # noqa: PLC0415

            if part.url:
                images.append(dspy.Image(part.url))
                continue
            if part.data:
                data = part.data
                if data.startswith("data:"):
                    images.append(dspy.Image(data))
                    continue
                media_type = part.media_type or part.metadata.get("media_type") or "image/png"
                images.append(dspy.Image(f"data:{media_type};base64,{data}"))
        except Exception:  # noqa: BLE001 - undecodable image part skipped
            continue
    return images


def _dspy_files_from_parts(
    parts: list[Part],
    *,
    app: Any | None = None,
    workspace_id: str = "",
) -> list[Any]:
    """Convert native-planned immutable PDF resources to DSPy file inputs.

    A PDF reaches the model only when the resource delivery planner selected
    ``native`` from a live provider handshake. Other documents stay on the
    existing structured-conversion and bounded-tool paths.
    """

    files: list[Any] = []
    for part in parts:
        if part.type != "resource_ref":
            continue
        resource_file = _resource_ref_file(part, app=app, workspace_id=workspace_id)
        if resource_file is not None:
            files.append(resource_file)
    return files
