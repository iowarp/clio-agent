"""A transcript ledger's tool_call parts by call id, for sealing them at their result.

Each tool result scanned every part of the turn to find its call (P7, 2026-11-02:
about 4,000 parts per result). :class:`ToolCallIndex` reads only the parts appended
since its last lookup. It remembers the last part it read, so a removal before it
(the ledger drops whitespace-only text parts) shifts positions and triggers a full
rebuild; in-place replacement only ever swaps delegation parts, never a tool call.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ToolCallIndex"]


class ToolCallIndex:
    """call_id -> tool_call parts of one ledger, kept in arrival order."""

    def __init__(self) -> None:
        self._parts: dict[str, list[Any]] = {}
        self._scanned = 0
        self._last: Any = None

    def parts(self, ledger: list[Any], call_id: str) -> list[Any]:
        """The ledger's tool_call parts for ``call_id`` (call under the ledger's lock)."""
        stale = self._scanned > len(ledger) or (
            self._scanned and ledger[self._scanned - 1] is not self._last
        )
        if stale:
            self._parts, self._scanned = {}, 0
        for part in ledger[self._scanned :]:
            if part.type == "tool_call" and part.call_id:
                self._parts.setdefault(part.call_id, []).append(part)
        self._scanned = len(ledger)
        self._last = ledger[-1] if ledger else None
        return self._parts.get(call_id, [])
