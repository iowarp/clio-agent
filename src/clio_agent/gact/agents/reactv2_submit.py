"""Submit-audit helpers for the CLIO ReActV2 loop."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def tool_names(tools: Iterable[Any]) -> list[str]:
    """Return stable names from DSPy tool-like objects."""

    return [name for tool in tools if (name := str(getattr(tool, "name", "") or "").strip())]


def active_react_scope_safe() -> str:
    """Return the active ReAct scope, or an empty value outside a live turn."""
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        return _ctx.active_react_scope()
    except Exception:  # noqa: BLE001 - scope is optional off-turn
        return ""


def record_submit_audit(
    reason: str,
    *,
    agent_id: str,
    field: str,
    text: str,
    suppressed: bool,
) -> None:
    """Emit one queryable V2-path stream-audit record."""
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        "bridge.contract_field",
        agent_id=agent_id or "",
        field=field,
        chunk_len=len(text),
        visible=False,
        duplicate_suppressed=suppressed,
        duplicate_reason=reason,
        head=text[:120],
        full_text=text[:12000],
    )
