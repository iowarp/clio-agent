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
  - semantic_event(event)                 → side-effects only
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

import hashlib
import importlib.util
import json
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


_KNOWN_EVENTS: tuple[str, ...] = (
    "pre_tool",
    "post_tool",
    "pre_message",
    "post_message",
    "semantic_event",
    "on_error",
)

_SCOPE_DIRS: tuple[tuple[str, str], ...] = (
    ("workspace_id", "workspaces"),
    ("session_id", "sessions"),
    ("blueprint_id", "blueprints"),
)


@dataclass(frozen=True)
class _HookHandler:
    fn: Callable[..., Any]
    path: Path
    scope: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


class HookInvocationError(PermissionError):
    """Permission-style hook failure that preserves invocation records."""

    def __init__(
        self,
        message: str,
        *,
        records: list[dict[str, Any]],
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.records = records
        self.original = original


def _default_hooks_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "clio-agent" / "hooks"
    return Path.home() / ".config" / "clio-agent" / "hooks"


class HookRegistry:
    """Loads + invokes user hooks for known events."""

    backend_name = "local_python"

    def __init__(self, *, hooks_dir: Optional[Path] = None, timeout_s: float | None = None) -> None:
        self._dir = hooks_dir if hooks_dir is not None else _default_hooks_dir()
        self._hooks: dict[str, list[_HookHandler]] = {event: [] for event in _KNOWN_EVENTS}
        self._lock = threading.Lock()
        self._timeout_s = (
            float(os.environ.get("CLIO_HOOK_TIMEOUT_S", "5.0"))
            if timeout_s is None
            else float(timeout_s)
        )
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clio-hooks")
        self._load()

    @property
    def hooks_dir(self) -> Path:
        return self._dir

    @property
    def event_names(self) -> tuple[str, ...]:
        return _KNOWN_EVENTS

    def _load(self) -> None:
        if not self._dir.exists():
            return
        self._load_paths(sorted(self._dir.glob("*.py")), scope={})
        for scope_key, folder_name in _SCOPE_DIRS:
            scope_root = self._dir / folder_name
            if not scope_root.exists():
                continue
            for scope_dir in sorted(path for path in scope_root.iterdir() if path.is_dir()):
                self._load_paths(
                    sorted(scope_dir.glob("*.py")),
                    scope={scope_key: scope_dir.name},
                )

    def _load_paths(self, paths: list[Path], *, scope: dict[str, str]) -> None:
        for path in paths:
            try:
                module = self._import_path(path)
            except Exception as exc:
                logger.warning("[clio-hooks] failed to load %s: %r", path, exc)
                continue
            for event in _KNOWN_EVENTS:
                fn = getattr(module, event, None)
                if callable(fn):
                    self._hooks[event].append(
                        _HookHandler(
                            fn=fn,
                            path=path,
                            scope=dict(scope),
                            provenance=self._provenance_for_path(path, event=event, scope=scope),
                        )
                    )

    @staticmethod
    def _checksum(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _metadata_sidecar(path: Path) -> Path:
        return path.with_name(f"{path.name}.json")

    def _provenance_for_path(
        self,
        path: Path,
        *,
        event: str,
        scope: dict[str, str],
    ) -> dict[str, Any]:
        sidecar = self._metadata_sidecar(path)
        metadata: dict[str, Any] = {}
        if sidecar.is_file():
            try:
                raw = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    metadata = {str(key): value for key, value in raw.items()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[clio-hooks] failed to read hook metadata %s: %r", sidecar, exc)
        provenance = {
            "backend": self.backend_name,
            "event": event,
            "hook_path": str(path),
            "installed_path": str(path),
            "checksum": self._checksum(path),
            "scope": dict(scope),
        }
        provenance.update(metadata)
        provenance["backend"] = self.backend_name
        provenance["event"] = event
        provenance["hook_path"] = str(path)
        provenance["installed_path"] = str(path)
        provenance["checksum"] = self._checksum(path)
        provenance["scope"] = dict(scope)
        return provenance

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

    def fire(self, event: str, /, *args: Any, **kwargs: Any) -> list[Any]:
        """Invoke every registered hook for ``event`` in load order.

        Returns the list of return values for hooks that didn't
        raise. Re-raises the FIRST PermissionError uncaught (the
        gate convention); other exceptions are wrapped as
        PermissionError so destructive callers can treat hook
        failures uniformly. ``post_*`` / ``semantic_event`` events
        swallow exceptions entirely (audit/log only — must not crash
        a turn).
        """

        return self.fire_with_records(event, *args, **kwargs)["results"]

    def matching_handlers(
        self,
        event: str,
        *,
        hook_scope: Optional[dict[str, Any]] = None,
        args: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        with self._lock:
            handlers = list(self._hooks.get(event, ()))
        resolved_scope = dict(hook_scope or self._infer_scope(event, args))
        return [
            dict(handler.provenance)
            for handler in handlers
            if self._scope_matches(handler.scope, resolved_scope)
        ]

    def fire_with_records(self, event: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Invoke hooks and return structured provenance/result records."""

        with self._lock:
            handlers = list(self._hooks.get(event, ()))
        hook_scope = dict(kwargs.pop("hook_scope", None) or self._infer_scope(event, args))
        handlers = [
            handler for handler in handlers if self._scope_matches(handler.scope, hook_scope)
        ]
        results: list[Any] = []
        records: list[dict[str, Any]] = []
        for handler in handlers:
            record = {
                **dict(handler.provenance),
                "status": "running",
            }
            try:
                result = self._call_with_timeout(handler.fn, *args, **kwargs)
                results.append(result)
                record["status"] = "completed"
                record["result_type"] = type(result).__name__
            except PermissionError as exc:
                record["status"] = "blocked"
                record["error"] = str(exc)
                records.append(record)
                if event.startswith("post_") or event in {"on_error", "semantic_event"}:
                    logger.warning("[clio-hooks] post-event hook raised; swallowing")
                    continue
                raise HookInvocationError(
                    str(exc),
                    records=records,
                    original=exc,
                ) from exc
            except Exception as exc:  # noqa: BLE001
                record["status"] = "failed"
                record["error"] = repr(exc)
                records.append(record)
                if event.startswith("post_") or event in {"on_error", "semantic_event"}:
                    logger.warning(
                        "[clio-hooks] post-event hook raised %r; swallowing",
                        exc,
                    )
                    continue
                raise HookInvocationError(
                    f"hook {handler.fn.__name__!r} for {event!r} raised: "
                    f"{exc!r}\n{traceback.format_exc(limit=3)}",
                    records=records,
                    original=exc,
                ) from exc
            records.append(record)
        return {"results": results, "handlers": records}

    def _call_with_timeout(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._timeout_s <= 0:
            return fn(*args, **kwargs)
        future = self._executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"hook {fn.__name__!r} exceeded timeout {self._timeout_s:g}s"
            ) from exc

    @staticmethod
    def _scope_matches(handler_scope: dict[str, str], hook_scope: dict[str, str]) -> bool:
        if not handler_scope:
            return True
        return all(str(hook_scope.get(key) or "") == value for key, value in handler_scope.items())

    @staticmethod
    def _infer_scope(event: str, args: tuple[Any, ...]) -> dict[str, str]:
        if event == "semantic_event" and args and isinstance(args[0], dict):
            payload = args[0]
            scope = {
                "workspace_id": str(payload.get("workspace_id") or ""),
                "session_id": str(payload.get("session_id") or ""),
            }
            blueprint = payload.get("blueprint")
            if isinstance(blueprint, dict):
                scope["blueprint_id"] = str(
                    blueprint.get("id")
                    or blueprint.get("agent_blueprint_id")
                    or blueprint.get("pack_id")
                    or ""
                )
            return {key: value for key, value in scope.items() if value}
        if event in {"pre_message", "post_message", "on_error"} and args:
            session_id = str(args[0] or "")
            return {"session_id": session_id} if session_id else {}
        return {}

    def count(self, event: str) -> int:
        with self._lock:
            return len(self._hooks.get(event, ()))

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            counts = {event: len(self._hooks.get(event, ())) for event in _KNOWN_EVENTS}
            scoped_counts: dict[str, int] = {}
            for handlers in self._hooks.values():
                for handler in handlers:
                    if not handler.scope:
                        continue
                    for key, value in handler.scope.items():
                        scoped_counts[f"{key}:{value}"] = scoped_counts.get(f"{key}:{value}", 0) + 1
        return {
            "backend": self.backend_name,
            "hooks_dir": str(self._dir),
            "events": list(_KNOWN_EVENTS),
            "handler_counts": counts,
            "scoped_handler_counts": scoped_counts,
            "enabled": True,
            "timeout_s": self._timeout_s,
            "failure_policy": {
                "pre_events": "fail_closed_permission_error",
                "post_events": "fail_open_warn",
                "semantic_event": "fail_open_swallow",
            },
        }


class DisabledHookRegistry(HookRegistry):
    """No-op registry used when hook dispatch is explicitly disabled."""

    backend_name = "none"

    def __init__(self) -> None:
        self._dir = Path("")
        self._hooks = {event: [] for event in _KNOWN_EVENTS}
        self._lock = threading.Lock()
        self._timeout_s = 0.0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-hooks-disabled")

    def _load(self) -> None:
        return

    def metadata(self) -> dict[str, Any]:
        data = super().metadata()
        data["enabled"] = False
        data["hooks_dir"] = ""
        return data


def _load_factory(path: str) -> Callable[..., Any]:
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("CLIO_HOOKS_FACTORY must be 'module.submodule:function'")
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    if not callable(factory):
        raise TypeError(f"hook factory is not callable: {path}")
    return factory


def build_hook_registry() -> HookRegistry:
    """Build the configured hook registry.

    Environment:
      - ``CLIO_HOOKS_BACKEND=local_python|none|factory``
      - ``CLIO_HOOKS_DIR=/path/to/hooks`` for ``local_python``
      - ``CLIO_HOOKS_FACTORY=module:function`` for ``factory``

    A custom factory may return a ``HookRegistry``-compatible object with
    ``fire()``, ``count()``, and ``metadata()`` methods.
    """

    backend = os.environ.get("CLIO_HOOKS_BACKEND", "local_python").strip().lower()
    if backend in {"", "local", "local_python", "python", "file", "filesystem"}:
        raw_dir = os.environ.get("CLIO_HOOKS_DIR", "").strip()
        hooks_dir = Path(raw_dir).expanduser() if raw_dir else None
        return HookRegistry(hooks_dir=hooks_dir)
    if backend in {"none", "off", "disabled"}:
        return DisabledHookRegistry()
    if backend in {"factory", "python_factory", "custom"}:
        factory_path = os.environ.get("CLIO_HOOKS_FACTORY", "").strip()
        if not factory_path:
            raise ValueError("CLIO_HOOKS_FACTORY is required when CLIO_HOOKS_BACKEND=factory")
        factory = _load_factory(factory_path)
        registry = factory()
        if not all(callable(getattr(registry, name, None)) for name in ("fire", "count")):
            raise TypeError("hook factory must return a HookRegistry-compatible object")
        return registry
    raise ValueError(f"unsupported hook backend: {backend}")


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


def fire_with_records(event: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Dispatch hooks and return provenance records for semantic traces."""

    if _registry is None:
        return {"results": [], "handlers": []}
    dispatch = getattr(_registry, "fire_with_records", None)
    if callable(dispatch):
        return dispatch(event, *args, **kwargs)
    return {"results": _registry.fire(event, *args, **kwargs), "handlers": []}


def matching_handlers(
    event: str,
    *,
    hook_scope: Optional[dict[str, Any]] = None,
    args: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Return handler provenance for hooks that would run for this scope."""

    if _registry is None:
        return []
    describe = getattr(_registry, "matching_handlers", None)
    if callable(describe):
        return describe(event, hook_scope=hook_scope, args=args)
    return []
