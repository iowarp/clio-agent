"""Project tool execution uncertainty from recorded results and live ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MISSING_EXECUTION_OUTCOME = (
    "Execution outcome unavailable: the turn ended before CLIO recorded this "
    "tool's result. Inspect any captured output before rerunning it."
)


def tool_execution_state(
    wire: Mapping[str, Any], part: Mapping[str, Any], active_message_ids: frozenset[str] | None
) -> str:
    """Prefer actual results; absence of a live owner never proves execution success.

    None means the caller has no runtime ownership information. An empty set is
    authoritative: no message in this session currently owns live tool work.
    """
    if part.get("is_error"):
        return "failed"
    if part.get("type") == "tool_result":
        return "succeeded"
    ended = wire.get("stop_reason") in {"end_turn", "error", "cancelled", "failed", "interrupted"}
    owner_absent = (
        active_message_ids is not None and str(wire.get("id") or "") not in active_message_ids
    )
    return "unknown" if ended or owner_absent else "running"
