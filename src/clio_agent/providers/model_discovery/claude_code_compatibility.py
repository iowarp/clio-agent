"""Official catalog requirements applied to the actual selected Claude Code client."""

from __future__ import annotations

from typing import Any

from clio_agent.providers.components.client_binary import version_key


def models_for_client(
    models: list[dict[str, Any]], client_version: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Keep supported models and report unmet requirements as informational rejections.

    This compares catalog evidence with a version, without a model turn or any
    inference about account access. Models without a documented minimum retain
    their existing behavior. Input rows and the maintained catalog are unchanged.
    """
    current = version_key(client_version)
    accepted: list[dict[str, Any]] = []
    waiting: list[dict[str, str]] = []
    for model in models:
        minimum = str(model.get("minimum_client_version") or "")
        required = version_key(minimum) if minimum else None
        if required is None or (current is not None and current >= required):
            accepted.append(dict(model))
            continue
        waiting.append(
            {
                "id": str(model["id"]),
                "code": "client_update_required" if current else "client_version_unknown",
                "minimum_client_version": minimum,
                "reason": f"{model['name']} needs Claude Code {minimum} or later; "
                "check again after an official Anthropic client update.",
            }
        )
    return accepted, waiting
