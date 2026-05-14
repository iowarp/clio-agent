"""User-defined hooks subsystem (iowarp/clio-agent#20).

Discovers Python files in ``~/.config/clio-agent/hooks/<event>.py``
and invokes them at lifecycle points. Lets users add project-
specific guardrails (extra path checks, audit logging, refusing
unsafe arguments) without rebuilding CLIO.

Supported events:
  - pre_tool(name, args)       → may raise PermissionError to block
  - post_tool(name, args, result, error?) → side-effects only
  - pre_message(session_id, text)         → may raise to block
  - post_message(session_id, assistant)   → side-effects only
  - on_error(session_id, error)           → side-effects only

Hook files expose top-level functions named after the event. Each
hook is loaded once at boot + cached; reloads require restart
(deliberate — no live module-replacement gymnastics).

Hook errors raised inside a hook propagate to the caller; raised
PermissionError stays as-is (the gate's standard signal). Other
exceptions get wrapped as PermissionError so the caller's
permission-handling path catches them uniformly.

Test-time: ``HookRegistry`` is constructed standalone; tests pass
their own dir + factories. Production: GACT's build_app loads from
the XDG path automatically.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


_KNOWN_EVENTS: tuple[str, ...] = (
    "pre_tool",
    "post_tool",
    "pre_message",
    "post_message",
    "on_error",
)


def _default_hooks_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "clio-agent" / "hooks"
    return Path.home() / ".config" / "clio-agent" / "hooks"


class HookRegistry:
    """Loads + invokes user hooks for known events."""

    def __init__(self, *, hooks_dir: Optional[Path] = None) -> None:
        self._dir = hooks_dir if hooks_dir is not None else _default_hooks_dir()
        self._hooks: dict[str, list[Callable[..., Any]]] = {
            event: [] for event in _KNOWN_EVENTS
        }
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._dir.exists():
            return
        for path in sorted(self._dir.glob("*.py")):
            try:
                module = self._import_path(path)
            except Exception as exc:
                logger.warning(
                    "[clio-hooks] failed to load %s: %r", path, exc
                )
                continue
            for event in _KNOWN_EVENTS:
                fn = getattr(module, event, None)
                if callable(fn):
                    self._hooks[event].append(fn)

    @staticmethod
    def _import_path(path: Path) -> Any:
        # Sandboxed-ish import: a unique module name per file path
        # so reloads don't shadow each other across HookRegistry
        # instances in tests.
        mod_name = f"_clio_hooks_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    def fire(
        self, event: str, /, *args: Any, **kwargs: Any
    ) -> list[Any]:
        """Invoke every registered hook for ``event`` in load order.

        Returns the list of return values for hooks that didn't
        raise. Re-raises the FIRST PermissionError uncaught (the
        gate convention); other exceptions are wrapped as
        PermissionError so destructive callers can treat hook
        failures uniformly. ``post_*`` events swallow exceptions
        entirely (audit/log only — must not crash a turn).
        """

        with self._lock:
            handlers = list(self._hooks.get(event, ()))
        results: list[Any] = []
        for fn in handlers:
            try:
                results.append(fn(*args, **kwargs))
            except PermissionError:
                if event.startswith("post_") or event == "on_error":
                    logger.warning(
                        "[clio-hooks] post-event hook raised; swallowing"
                    )
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                if event.startswith("post_") or event == "on_error":
                    logger.warning(
                        "[clio-hooks] post-event hook raised %r; swallowing",
                        exc,
                    )
                    continue
                raise PermissionError(
                    f"hook {fn.__name__!r} for {event!r} raised: "
                    f"{exc!r}\n{traceback.format_exc(limit=3)}"
                ) from exc
        return results

    def count(self, event: str) -> int:
        with self._lock:
            return len(self._hooks.get(event, ()))


# Module-level singleton wired by GACT's build_app.
_registry: Optional[HookRegistry] = None


def install_global_registry(reg: Optional[HookRegistry]) -> None:
    """Install (or clear) the process-global hook registry."""

    global _registry
    _registry = reg


def fire(event: str, /, *args: Any, **kwargs: Any) -> list[Any]:
    """Module-level convenience: dispatch to the installed registry,
    or no-op when no registry is wired.

    Production code path: app.state-side hooks fire through this so
    callers don't need a HookRegistry reference. Tests construct
    their own registry + use ``reg.fire`` directly.
    """

    if _registry is None:
        return []
    return _registry.fire(event, *args, **kwargs)
