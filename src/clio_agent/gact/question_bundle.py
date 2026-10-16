"""Native question bundles: one durable question and one atomic answer."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException


def normalize_question_bundle(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a bundle before publishing any question or pausing work."""
    from clio_agent.gact.ask_user_tool import (
        AskUserError,
        _normalize_options,
        _validated_question,
    )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in questions:
        if not isinstance(item, Mapping):
            raise AskUserError("Bundle questions must be objects.")
        key = item.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise AskUserError("Each bundle question needs a stable id (letters, digits, _ or -).")
        if key in seen:
            raise AskUserError(f"Duplicate bundle question id: {key}.")
        seen.add(key)
        kind = item.get("kind", "freeform")
        if not isinstance(kind, str) or kind not in {"freeform", "choice", "confirmation"}:
            raise AskUserError("Bundle question kind must be freeform, choice, or confirmation.")
        options = _normalize_options(item.get("options") or [])
        if kind == "confirmation" and not options:
            options = _normalize_options(
                [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
            )
        if kind == "choice" and not options:
            raise AskUserError("Bundle choice questions require options.")
        if kind == "freeform" and options:
            raise AskUserError("Use kind=choice for bundle questions with options.")
        if not isinstance(item.get("allowFreeform", False), bool):
            raise AskUserError("allowFreeform must be a boolean.")
        normalized.append(
            {
                "id": key,
                "question": _validated_question(item.get("question", "")),
                "kind": kind,
                "options": options,
                "allow_freeform": kind == "freeform" or item.get("allowFreeform", False),
            }
        )
    if not normalized:
        raise AskUserError("A question bundle must contain at least one question.")
    # Fields keep older clients capable of answering ordinary choice/text bundles.
    fields = [
        {
            "name": item["id"],
            "title": item["question"],
            "type": "string",
            "required": True,
            **(
                {
                    "enum": [o["value"] for o in item["options"]],
                    "enum_names": [o["label"] for o in item["options"]],
                }
                if item["options"]
                else {}
            ),
        }
        for item in normalized
    ]
    return {"questions": normalized, "fields": fields}


def validate_question_answer(question: Any, answer: str, metadata: dict[str, Any]) -> str:
    """Reject partial/invalid submissions before the pending-to-answered claim.

    Return the complete human-readable answer for a bundle, preserving the text
    of ordinary questions. Legacy clients can submit the projected flat fields.
    """
    bundle = question.metadata.get("question_bundle")
    if not isinstance(bundle, Mapping):
        return answer
    items = bundle["questions"]
    answers = metadata.get("answers")
    if answers is None:
        answers = {
            item["id"]: {"answer": metadata[item["id"]]} for item in items if item["id"] in metadata
        }
    errors: dict[str, str] = {}
    lines: list[str] = []
    if not isinstance(answers, Mapping):
        raise HTTPException(status_code=422, detail="Submit all bundle answers together.")
    if set(answers) - {item["id"] for item in items}:
        errors["answers"] = "Unknown question id."
    for item in items:
        key = item["id"]
        value = answers.get(key)
        if not isinstance(value, Mapping):
            errors[key] = "An answer is required."
            continue
        text = value.get("answer", "")
        selected = value.get("selected_options", [])
        if (
            not isinstance(text, str)
            or not isinstance(selected, list)
            or any(not isinstance(s, str) for s in selected)
            or len(selected) > 1
        ):
            errors[key] = "Provide text or one selected option."
            continue
        text = text.strip()
        options = {o["value"]: o["label"] for o in item["options"]}
        if not selected and text in options:
            selected, text = [text], ""
        if selected and selected[0] not in options:
            errors[key] = "Select an offered option."
        elif text and not item["allow_freeform"]:
            errors[key] = "Select an offered option."
        elif not selected and not text:
            errors[key] = "An answer is required."
        else:
            answer = options[selected[0]] if selected else ""
            lines.append(
                f"{key}. {item['question']}\n{answer}{': ' if answer and text else ''}{text}"
            )
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "error": "invalid_question_bundle",
                    "message": "Answer every question before submitting.",
                    "details": {"field_errors": errors},
                    "recoverable": True,
                },
            },
        )
    return "\n\n".join(lines)
