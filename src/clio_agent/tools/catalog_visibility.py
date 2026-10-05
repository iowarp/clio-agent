"""Project expert tool declarations into catalog visibility scopes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def expert_visibility(experts: Iterable[Any] | None) -> dict[str, set[str]]:
    """Map tool names to declaring experts and optional planner visibility."""
    visible: dict[str, set[str]] = {}
    for expert in experts or []:
        expert_id = str(getattr(expert, "id", "") or "").strip()
        if not expert_id:
            continue
        metadata = getattr(expert, "metadata", {}) or {}
        planner_visible = bool(metadata.get("planner_visible", True))
        for tool_name in getattr(expert, "tools", []) or []:
            name = str(tool_name).strip()
            if not name:
                continue
            scopes = visible.setdefault(name, set())
            scopes.add(expert_id)
            if planner_visible:
                scopes.add("planner")
    return visible
