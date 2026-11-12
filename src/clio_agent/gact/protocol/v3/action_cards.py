"""Action cards on a message part (v3 projection): what each offered action does.

An ``inspect_attention`` action carries its evidence receipt, validated against the
``clio_schemas`` contract; an invalid receipt disables the card and says why.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_schemas.attention_evidence import AttentionEvidenceInspection
from pydantic import ValidationError


def action_cards(part: Mapping[str, Any]) -> list[dict[str, Any]]:
    actions = part.get("actions")
    if not isinstance(actions, list):
        return []
    projected: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping) or not action.get("id"):
            continue
        behavior = action.get("behavior")
        projected_behavior: dict[str, Any] = {
            "kind": str(behavior.get("kind") or "unavailable")
            if isinstance(behavior, Mapping)
            else "unavailable"
        }
        if isinstance(behavior, Mapping):
            if behavior.get("handle_id"):
                projected_behavior["handle_id"] = str(behavior["handle_id"])
            if behavior.get("reason"):
                projected_behavior["reason"] = str(behavior["reason"])
            if behavior.get("kind") == "inspect_attention" and behavior.get("inspection"):
                try:
                    projected_behavior["inspection"] = AttentionEvidenceInspection.model_validate(
                        behavior["inspection"]
                    ).model_dump()
                except ValidationError:
                    projected_behavior["reason"] = "The finding has an invalid evidence receipt."
        projected.append(
            {
                "id": str(action["id"]),
                "label": str(action.get("label") or action["id"]),
                "enabled": bool(action.get("enabled", True))
                and (
                    projected_behavior["kind"] != "inspect_attention"
                    or "inspection" in projected_behavior
                ),
                "behavior": projected_behavior,
            }
        )
    return projected
