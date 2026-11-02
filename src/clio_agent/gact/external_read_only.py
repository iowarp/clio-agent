"""Read-only MCP calls go out at once; their admission is recorded with the outcome.

Every MCP call used to be write-ahead: its submission and first snapshot were
confirmed in clio-core before ``tools/call`` was sent, so recovery could reconnect
to a server-created task through ``tasks/get`` instead of resubmitting. For an
inline call that record's only use after a crash is "acceptance identity unknown;
nothing was resubmitted". The owner decision (2026-11-01, arc performance review
(b)1) keeps write-ahead for calls that can change something and lifts it for
read-only ones: a crash during one leaves no record, and nothing needs recovering.

Measured cost of keeping it (P7, 2026-11-02): a script gathered 100 ``get_record``
calls and CLIO allowed 9 in flight, but each waited for its submission to be
confirmed through the single history writer, a median 46 ms apart, so the server
received them strictly one at a time.

A call skips write-ahead only when the server declares the tool read-only
(:func:`~clio_agent.tools.catalog.annotations_are_read_only`) and does not declare
task support, so the call is not task-augmented, and ``tools.write_ahead`` is
``non_read_only`` (``all`` restores write-ahead for every call). The submission's
content reference is content-addressed, so the call is bound to it before it is
written; the admission (submission plus first snapshot) is then written once, in
the same history-writer turn as the outcome: the inline result, the rejection, the
attention record, or a task the server created anyway (adopted as before).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from clio_agent import conf

__all__ = ["WRITE_AHEAD_MODES", "LazyAdmission", "skips_write_ahead", "write_ahead_mode"]

#: ``tools.write_ahead`` values: which MCP calls confirm their submission before sending.
WRITE_AHEAD_MODES = ("non_read_only", "all")


def write_ahead_mode() -> str:
    """Which MCP calls are write-ahead (``tools.write_ahead``).

    Raises:
        ValueError: The configured value is not one of :data:`WRITE_AHEAD_MODES`.
    """
    value = conf.resolve(
        "tools.write_ahead", env="CLIO_TOOLS_WRITE_AHEAD", default="non_read_only", cast=conf.as_str
    )
    if value not in WRITE_AHEAD_MODES:
        raise ValueError(f"tools.write_ahead must be one of {WRITE_AHEAD_MODES}, not {value!r}")
    return value


def _task_support(tool: Any) -> str | None:
    execution = getattr(tool, "execution", None)
    if execution is None:
        return None
    return getattr(execution, "taskSupport", None) or getattr(execution, "task_support", None)


def skips_write_ahead(tool: Any) -> bool:
    """Whether a listed MCP tool's calls are sent before their admission is recorded."""
    from clio_agent.tools.catalog import annotations_are_read_only  # noqa: PLC0415 - import cost

    if write_ahead_mode() == "all" or _task_support(tool) not in (None, "forbidden"):
        return False
    annotations = getattr(tool, "annotations", None)
    if annotations is not None and hasattr(annotations, "model_dump"):
        annotations = annotations.model_dump(by_alias=True, exclude_none=True)
    return annotations_are_read_only(annotations)


class LazyAdmission:
    """One read-only call's admission, written at most once, before its outcome.

    Args:
        admit: Writes the submission and first snapshot (the write-ahead ``confirm``).
            Run only on the history writer.
    """

    def __init__(self, admit: Callable[[], Any]) -> None:
        self._admit = admit
        self._lock = threading.Lock()
        self.recorded = False

    def ensure(self) -> None:
        """Record the admission if it is not recorded yet."""
        with self._lock:
            if not self.recorded:
                self._admit()
                self.recorded = True

    def then(self, write: Callable[[], Any]) -> Callable[[], Any]:
        """``write`` preceded by the admission, as one history-writer operation."""

        def admitted() -> Any:
            self.ensure()
            return write()

        return admitted
