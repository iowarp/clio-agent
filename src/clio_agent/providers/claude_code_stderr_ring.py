"""A bounded per-client stderr ring buffer for the ``claude_code`` SDK transport (B17).

The Agent SDK pipes the CLI subprocess's stderr to CLIO only when
``ClaudeAgentOptions.stderr`` is set (see ``claude_agent_sdk._internal.transport
.subprocess_cli``: the pipe is opened `iff` a callback is registered). Without
it, a CLI crash surfaces to CLIO only as an opaque ``ProcessError``/exit code --
whatever the CLI itself printed about *why* it died is lost. This module is the
owner of the small, bounded buffer each pooled client attaches its callback to,
so a crash error can carry the CLI's own last words instead of just its exit
code.

Bounded, never unbounded: a misbehaving CLI that spews stderr must not turn
into an unbounded per-client memory leak. Only the LAST :data:`RING_CAPACITY`
lines are kept (a ``collections.deque(maxlen=...)``), which is exactly what a
crash diagnosis needs -- the tail, not the whole run.
"""

from __future__ import annotations

from collections import deque

__all__ = ["RING_CAPACITY", "StderrRing"]

#: Lines kept per client. Generous enough to catch a crash's actual error
#: message plus a few lines of context, small enough that even a
#: pathologically noisy CLI process costs only a few KB resident.
RING_CAPACITY = 200


class StderrRing:
    """A bounded FIFO of the most recent stderr lines from one CLI subprocess.

    One instance per pooled client (:class:`~clio_agent.providers.claude_code_sessions._StreamClientEntry`),
    replaced whenever the client itself is replaced (a fresh subprocess starts
    with an empty ring -- stale stderr from a PRIOR process must never be
    attributed to a new one's crash).
    """

    __slots__ = ("_lines",)

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=RING_CAPACITY)

    def append(self, line: str) -> None:
        """Record one stderr line (called from the SDK's ``stderr`` callback)."""
        stripped = line.rstrip("\n")
        if stripped:
            self._lines.append(stripped)

    def tail(self, n: int = RING_CAPACITY) -> str:
        """Return the last ``n`` recorded lines joined with newlines (``""`` if empty)."""
        if n >= len(self._lines):
            return "\n".join(self._lines)
        return "\n".join(list(self._lines)[-n:])

    def clear(self) -> None:
        """Drop every recorded line (reused before a client reconnects)."""
        self._lines.clear()

    def __len__(self) -> int:
        return len(self._lines)
