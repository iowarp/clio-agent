"""What a person's summons and typed commands show on their message (v3 projection).

Only the public names cross: the subagents, workflows and flows the person asked
for and the servers they pointed the message at; on the turn's answer, which of
those its calls started and which they did not, and why; the skill a ``/<skill>``
loaded, and an MCP prompt command's server, prompt and arguments with the text it
fetched. Task handles and model context stay on the server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def summon_presentation(metadata: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    summons = metadata.get("summons")
    if isinstance(summons, Mapping):
        projected["summons"] = {
            "subagents": [str(row.get("role") or "") for row in _rows(summons.get("subagents"))],
            "servers": [str(name) for name in summons.get("servers") or [] if name],
            "workflows": [str(name) for name in summons.get("workflows") or [] if name],
            "flows": [str(name) for name in summons.get("flows") or [] if name],
            "failed": [
                {"role": str(row.get("role") or ""), "error": str(row.get("error") or "")}
                for row in _rows(summons.get("failed"))
            ],
        }
    outcome = metadata.get("summons_outcome")
    if isinstance(outcome, Mapping):
        projected["summons_outcome"] = {
            key: [
                {
                    "kind": str(row.get("kind") or ""),
                    "target": str(row.get("target") or ""),
                    **({"reason": str(row.get("reason") or "")} if key == "not_started" else {}),
                }
                for row in _rows(outcome.get(key))
            ]
            for key in ("started", "not_started")
        }
    if isinstance(metadata.get("loaded_skill"), str) and metadata["loaded_skill"]:
        projected["loaded_skill"] = metadata["loaded_skill"]
    prompt = metadata.get("mcp_prompt")
    if isinstance(prompt, Mapping):
        projected["mcp_prompt"] = {
            "server": str(prompt.get("server") or ""),
            "prompt": str(prompt.get("prompt") or ""),
            "arguments": {str(k): str(v) for k, v in (prompt.get("arguments") or {}).items()},
            "text": str(prompt.get("text") or ""),
        }
    return projected


__all__ = ["summon_presentation"]
