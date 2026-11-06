"""What a person's summons and typed commands show on their message (v3 projection).

Only the public names cross: the subagents the person started (with any that
could not start and why), the servers they pointed the message at, the skill a
``/<skill>`` loaded, and an MCP prompt command's server, prompt and arguments
with the text it fetched. Task handles and model context stay on the server.
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
            "failed": [
                {"role": str(row.get("role") or ""), "error": str(row.get("error") or "")}
                for row in _rows(summons.get("failed"))
            ],
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
