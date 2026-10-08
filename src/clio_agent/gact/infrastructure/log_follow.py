"""Follow a growing log file on a target through the ordinary command executor.

A supervised install or server writes its output to a file on the target
(``logs/install.log``); the runtime reads what is new on every readiness poll
with one small ``sh`` command, by byte offset, in chunks well under the
executor's 16 kB output tail, so the live log does not depend on that tail.
Only whole lines are delivered until the final drain; a file that shrank (a
new run truncated it) is read again from its start.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec

#: Bytes read per command (the executor keeps 16 000 characters).
CHUNK_BYTES = 12_000
_HEADER = "CLIO_LOG_SPAN "
_SCRIPT = (
    'f=$0; o=$1; cap=$2; if [ ! -f "$f" ]; then echo "CLIO_LOG_SPAN 0 0 0"; exit 0; fi; '
    's=$(wc -c < "$f" | tr -d " "); if [ "$s" -lt "$o" ]; then o=0; fi; '
    "e=$((o + cap)); if [ \"$e\" -gt \"$s\" ]; then e=$s; fi; "
    'echo "CLIO_LOG_SPAN $o $e $s"; tail -c +$((o + 1)) "$f" | head -c $((e - o))'
)


def follow_command(path: str, offset: int, cap: int = CHUNK_BYTES) -> CommandSpec:
    """Read up to ``cap`` bytes of ``path`` from byte ``offset``."""

    return CommandSpec(
        program="sh", args=["-c", _SCRIPT, path, str(max(0, offset)), str(cap)], timeout_seconds=30
    )


def parse_span(result: CommandResult) -> tuple[int, int, int, str] | None:
    """(start, end, size, text) of one read, or None when the read failed."""

    if result.exit_code != 0:
        return None
    header, _, text = result.stdout.partition("\n")
    if not header.startswith(_HEADER):
        return None
    try:
        start, end, size = (int(part) for part in header[len(_HEADER) :].split())
    except ValueError:
        return None
    return start, end, size, text


class LogFollower:
    """Deliver the new whole lines of one target file on every :meth:`poll`."""

    def __init__(self, path: str, *, rounds: int = 8) -> None:
        self.path = path
        self.offset = 0
        self.rounds = rounds

    async def poll(
        self,
        execute: Callable[[CommandSpec], Awaitable[CommandResult]],
        sink: Callable[[str], None],
        *,
        final: bool = False,
    ) -> None:
        """Read what is new (up to ``rounds`` chunks) and hand it to ``sink``."""

        for _ in range(self.rounds):
            try:
                span = parse_span(await execute(follow_command(self.path, self.offset)))
            except (OSError, RuntimeError, ValueError):
                return
            if span is None:
                return
            start, end, size, text = span
            if end <= start:
                return
            if end < size or not final:
                cut = text.rfind("\n")
                if cut < 0 and end >= size:
                    return  # one unfinished line: wait for the rest of it
                if cut >= 0:
                    text = text[: cut + 1]
                    end = start + len(text.encode("utf-8"))
            self.offset = end
            if text:
                sink(text)
            if end >= size:
                return


__all__ = ["CHUNK_BYTES", "LogFollower", "follow_command", "parse_span"]
