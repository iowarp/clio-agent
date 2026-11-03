"""A "did you mean" hint for a failed tool call's missing path arguments.

The harness guides, it never decides: the call already ran exactly as the agent
asked; when it failed, this tells the agent which same-named files exist under the
allowed roots, and the agent chooses (owner decision, 2026-09-29).

Each line is the reminder ``clio.reminder.path_hint`` (:mod:`clio_agent.gact.reminders`):
a person can reword it or switch it off, and switched off no hint is looked for.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import FileAccessPolicy

logger = logging.getLogger(__name__)

__all__ = ["PATH_HINT_MARKER", "PATH_HINT_SOURCE", "missing_path_hint"]

#: The injection's source and its reminder (``clio.reminder.path_hint``).
PATH_HINT_SOURCE = "path_hint"
#: Heads the hint; the transcript and other code recognise it by this.
PATH_HINT_MARKER = "[clio: path_hint]\n"

_FILE_ARGUMENT_NAMES = {
    "file",
    "filepath",
    "file_path",
    "path",
    "input",
    "input_path",
    "source",
    "source_path",
}

# Bounds on the allowed-root basename scan behind a path hint: a mistyped path must
# not turn a failed call into an unbounded filesystem walk (a partial scan hints less).
_HINT_SCAN_LIMIT = 20_000
_HINT_DEADLINE_S = 2.0
_HINT_MATCHES = 3  # the "did you mean" options offered per argument


def _bounded_basename_matches(
    roots: Sequence[Path],
    basename: str,
    scanned: int,
    deadline: float,
) -> tuple[list[Path], int, bool]:
    """Walk ``roots`` for files named ``basename``, bounding every entry visited.

    Unlike ``Path.rglob``, which only yields name-matches (so a no-match basename
    over a huge tree would traverse it exhaustively before any bound could be
    consulted), this walk increments ``scanned`` and checks the wall-clock
    ``deadline`` for EVERY directory entry visited. Directory symlinks are not
    followed, matching ``rglob``'s non-recursing behavior and avoiding cycles.

    Returns:
        ``(matches, scanned, aborted)``: resolved file matches (the walk stops
        at ``_HINT_MATCHES``), the updated
        entry count, and whether a bound aborted the walk.
    """
    matches: list[Path] = []
    for root in roots:
        stack: list[str] = [str(root)]
        while stack:
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _HINT_SCAN_LIMIT or time.monotonic() > deadline:
                        return matches, scanned, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name == basename and entry.is_file():
                            matches.append(Path(entry.path).resolve())
                            if len(matches) >= _HINT_MATCHES:
                                return matches, scanned, False
                    except OSError:
                        continue
    return matches, scanned, False


def missing_path_hint(args: Mapping[str, Any]) -> str:
    """A "did you mean" hint for path arguments of a failed call that do not exist.

    The harness guides, it never decides: the call already ran as the agent asked;
    this only tells the agent which same-named files exist under the allowed roots
    (bounded walk, at most ``_HINT_MATCHES`` each). Empty when there is nothing to say,
    or when the person switched the reminder off.
    """
    from clio_agent.gact import reminders  # noqa: PLC0415 - tools load before gact

    if not reminders.active(PATH_HINT_SOURCE):
        return ""
    try:
        policy = FileAccessPolicy.from_env()
    except Exception as exc:  # noqa: BLE001 - degradation surfaced via structured log below
        logger.warning(
            "path hint skipped: file policy unavailable reason=file_policy_unavailable error=%r",
            exc,
        )
        return ""
    lines: list[str] = []
    scanned = 0
    deadline = time.monotonic() + _HINT_DEADLINE_S
    for key, value in args.items():
        if key not in _FILE_ARGUMENT_NAMES or not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if candidate.exists() or candidate.name in {"", ".", ".."}:
            continue
        matches, scanned, _aborted = _bounded_basename_matches(
            policy.allowed_roots, candidate.name, scanned, deadline
        )
        if matches:
            options = " or ".join(f"'{m}'" for m in sorted(set(matches)))
            line = reminders.render(
                PATH_HINT_SOURCE, {"argument": key, "value": value, "options": options}
            )
            if line:
                lines.append(line)
    return PATH_HINT_MARKER + "\n".join(lines) if lines else ""
