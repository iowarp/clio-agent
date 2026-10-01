"""The loud DSPy ``History`` mode: the one sanctioned fallback (owner module).

clio-core is THE context system. When the platform cannot run it at all -- its Python
binding (``iowarp_core`` + ``clio_cte_core_ext``) is not installed -- CLIO runs on DSPy
``History`` instead: the agent's context is held in memory for the forward and rebuilt
from the transcript file, nothing is durable, and every surface says so (health, doctor,
the UI, each highway event). This is decided ONCE per process, from the platform, before
any store is built:

* the binding is present -> clio-core mode; any later clio-core failure (spawn, attach,
  durability, capacity, version, a broken install) is a typed error, never this mode;
* the binding is absent -> History mode, logged at ERROR, counted for the test guard.

A process never switches modes.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from clio_agent.errors import ClioError

logger = logging.getLogger(__name__)

BINDING_MODULES = ("iowarp_core", "clio_cte_core_ext")
HISTORY_MODE_REASON = "clio_core_binding_absent"
_REMEDY = "install a build of iowarp-core for this platform, then restart CLIO"


@dataclass(frozen=True)
class ContextMode:
    """Where the agent's context lives for this process."""

    mode: Literal["clio_core", "history"]
    reason: str = ""
    detail: str = ""

    @property
    def is_history(self) -> bool:
        """True in the loud DSPy ``History`` mode."""
        return self.mode == "history"

    def as_dict(self) -> dict[str, str]:
        """The mode as health/UI fields."""
        out: dict[str, str] = {"context_mode": self.mode}
        if self.is_history:
            out.update(reason=self.reason, detail=self.detail, remedy=_REMEDY)
        return out


CLIO_CORE = ContextMode("clio_core")

_lock = threading.Lock()
_mode: ContextMode | None = None
_entries = 0


class HistoryModeUnsupportedError(ClioError):
    """An operation that needs clio-core was asked for in History mode."""

    reason = "history_mode_unsupported"

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"{operation} needs clio-core, and this CLIO runs in History mode "
            f"(the clio-core binding is not installed: {_REMEDY})",
            error_type=self.reason,
            details={"operation": operation, "context_mode": "history"},
        )


class ArcNotBoundError(ClioError):
    """An operation needed the ARC, none is bound, and History mode is not active."""

    reason = "arc_not_bound"

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"{operation} needs clio-core, and no clio-core store is bound",
            error_type=self.reason,
            details={"operation": operation},
        )


def binding_present() -> bool:
    """Whether the clio-core Python binding is installed (no module is executed)."""
    return all(importlib.util.find_spec(name) is not None for name in BINDING_MODULES)


def resolve() -> ContextMode:
    """Decide this process's context mode once, from the platform; return it."""
    global _mode, _entries
    with _lock:
        if _mode is None:
            if binding_present():
                _mode = CLIO_CORE
            else:
                missing = [n for n in BINDING_MODULES if importlib.util.find_spec(n) is None]
                _mode = ContextMode(
                    "history",
                    reason=HISTORY_MODE_REASON,
                    detail=f"not importable: {', '.join(missing) or 'clio-core binding'}",
                )
                _entries += 1
                logger.error(
                    "CLIO runs in History mode: %s. The agent's context is held in memory "
                    "only (nothing durable, no context edits); %s.",
                    _mode.detail,
                    _REMEDY,
                )
        return _mode


def active() -> bool:
    """True once this process has entered History mode (never decides by itself)."""
    return _mode is not None and _mode.is_history


def require_arc(arc: Any, operation: str) -> Any:
    """Return ``arc``; a missing one is :class:`HistoryModeUnsupportedError` in History
    mode, else :class:`ArcNotBoundError`."""
    if arc is None and active():
        raise HistoryModeUnsupportedError(operation)
    if arc is None:
        raise ArcNotBoundError(operation)
    return arc


def entries() -> int:
    """How many times this process entered History mode (monotonic; the test guard)."""
    return _entries


def reset_for_tests() -> None:
    """Forget the decision so the next :func:`resolve` decides again (tests only)."""
    global _mode
    with _lock:
        _mode = None
