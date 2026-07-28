"""Message / multimodal + ask-user helpers for the GACT server (#714).

Behavior-preserving extraction from :mod:`clio_agent.gact.app`. This module owns
the cohesive cluster that shapes a turn's *message I/O* — building transcript
parts, bridging image parts to native-vision model inputs, coercing/rendering
ask-user planner actions, and summarizing predictions/subagent inputs for the
trace. It is the single source of truth for:

* **Multimodal message parts** — whether an agent's ``forward`` accepts native
  images (:func:`_agent_accepts_images`), assembling transcript parts for a user
  turn while preserving image parts (:func:`_user_message_parts`), bounded
  image-part metadata for logging without raw base64
  (:func:`_image_part_summaries`), and converting GACT image parts to DSPy image
  inputs (:func:`_dspy_images_from_parts`).
* **Ask-user planner actions** — extracting an ask-user action from a prediction
  (:func:`_coerce_ask_user_action`), turning it into typed question options
  (:func:`_ask_user_options_from_action`), and rendering an answered question
  back into resume text (:func:`_ask_user_resume_text`).
* **Trace summaries** — the routing/predict trace payload for a prediction
  (:func:`_prediction_summary`) and a human-readable rendering of a materialized
  subagent input (:func:`_format_subagent_input`).

The module imports stdlib, the id/json runtime primitives from
:mod:`clio_agent.gact.runtime.globals` (``_new_part_id``, ``_jsonish``), and the
GACT message/question types. ``dspy`` is imported lazily inside
:func:`_dspy_images_from_parts` to keep this module free of any heavy engine
import at module top. It never imports :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.runtime.globals import _jsonish, _new_part_id
from clio_agent.gact.types import Part, UserQuestion, UserQuestionOption

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
        "expert_handoffs": _jsonish(getattr(pred, "expert_handoffs", None) or []),
        "tools_called": _jsonish(getattr(pred, "tools_called", None) or []),
        "file_diffs": _jsonish(getattr(pred, "file_diffs", None) or []),
        "error_info": _jsonish(getattr(pred, "error_info", None)),
    }
    # Full capture (durable trace): the dspy ReAct trajectory and the extract's
    # chain-of-thought reasoning. These are in SENSITIVE_KEYS, so the SSE
    # projection strips them while the canonical trace keeps them for debugging
    # and (later) re-extract repair. Only attach when present to keep the
    # routing/predict payloads lean.
    trajectory = getattr(pred, "trajectory", None)
    if trajectory:
        summary["trajectory"] = _jsonish(trajectory)
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
        if part.type not in {"text", "image", "artifact_review"}:
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


def _dspy_images_from_parts(parts: list[Part]) -> list[Any]:
    """Convert GACT image parts to DSPy image inputs for native vision models."""

    images: list[Any] = []
    for part in parts:
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
