"""Live workspace file-change watcher (F1, iowarp/clio-agent).

Owner module for the OS-level notification watcher backing the Files view.
Replaces the old client-side trigger list (a hand-maintained set of SSE event
names the Files view happened to refresh on) with a real filesystem watch: the
Rust ``notify`` backend via :mod:`watchfiles` (``ReadDirectoryChangesW`` on
Windows, ``inotify`` on Linux, ``FSEvents`` on macOS), so an upload landing in
``.clio/inputs``, the user's own Explorer edits, or a `git checkout` all
refresh the view — not just the fs/shell tool calls and turn boundaries the
old trigger list happened to name.

Design (⚑ CLAUDE.md superseding principles — no deterministic keyword/prose
decision-making in core; this module makes NO content decisions, it only
surfaces filesystem-change *facts*):

* **One watcher per root, refcounted.** :class:`WorkspaceWatchRegistry` starts
  exactly one :func:`watchfiles.awatch` task per workspace root the first time
  a live SSE subscriber attaches (``acquire``) and stops it once the last one
  disconnects (``release``) — never a fixed number of tiers, never polling.
  Multiple sessions in the same workspace share the one task.
* **Same filter as the listing.** The ignore rule is
  :func:`clio_agent.gact.routes.workspace_file_policy.skip_workspace_file_directory`
  — the identical closed set the ``@``-picker/Files walk uses (``.git``,
  ``node_modules``, ``.venv``, ...). This module does not invent a second
  ignore list, and does not skip ``.clio`` (uploads materialize into
  ``.clio/inputs`` and MUST be reported).
* **No silent fallback.** A watcher that cannot start (permission denied, an
  unsupported filesystem) records a typed reason
  (:data:`UNAVAILABLE_REASON`) via :func:`clio_agent.runtime.trace.event` and
  a ``logger.warning``, AND is exposed through :meth:`WorkspaceWatchRegistry.status`
  so the files-listing response can surface it to the client (a hover card,
  never a silently-stale view). This is the same *typed-reason* pattern the
  ``stream_fallback`` ledger (``gact/stream_fallbacks.py``) uses for LM
  streaming degradation; workspace watch failures are a different concern
  (filesystem I/O, not LM output shape) so they get their own small, honestly
  named reason rather than being folded into that unrelated catalog.
* **Batched, capped events.** Real filesystem notifications arrive in bursts
  (an editor's save-as-temp-then-rename, a multi-file `git checkout`).
  ``watchfiles`` debounces these into one batch (~300 ms); each batch becomes
  exactly one ``workspace.files.changed`` event on the existing session event
  bus, with workspace-relative paths capped at :data:`MAX_CHANGED_PATHS` and
  an honest ``truncated`` flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from watchfiles import Change, awatch

from clio_agent.gact.events import Event
from clio_agent.gact.routes.workspace_file_policy import skip_workspace_file_directory
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: SSE event type published once per debounced batch of filesystem changes.
EVENT_TYPE = "workspace.files.changed"

#: Typed reason recorded (trace + files-listing response) when a workspace's
#: watcher cannot run at all -- permission denied, an unsupported filesystem
#: (some network drives), or the root disappearing out from under it.
UNAVAILABLE_REASON = "workspace_watch_unavailable"

#: watchfiles batches changes that land within this window into one event
#: (owner spec: "~300 ms").
DEBOUNCE_MS = 300

#: A batch is capped at this many workspace-relative paths; ``truncated`` on
#: the wire event is honest about anything beyond the cap (mirrors the
#: workspace-file-listing walk's own truncation contract).
MAX_CHANGED_PATHS = 200


@dataclass
class WorkspaceWatchStatus:
    """Current health of one workspace root's watcher, wire-projectable."""

    active: bool = False
    reason: str | None = None
    detail: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Project to the shape the files-listing response embeds.

        ``reason``/``detail`` are omitted entirely when there is nothing to
        report -- a workspace with no live subscriber yet is simply
        ``{"active": False}``, never a fabricated "unavailable" claim.
        """

        payload: dict[str, Any] = {"active": self.active}
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass
class _WatchHandle:
    """One resident watcher: its task, refcount, and last-known status."""

    root: Path
    refcount: int = 0
    task: "asyncio.Task[None] | None" = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: WorkspaceWatchStatus = field(default_factory=WorkspaceWatchStatus)


def resolve_workspace_root(app: "FastAPI", workspace_id: str) -> Path | None:
    """Return the live, existing root directory for ``workspace_id``, or ``None``.

    Shared by the SSE subscribe hook (:mod:`clio_agent.gact.routes.misc`) so
    that route does not duplicate the workspace-lookup + ``expanduser`` +
    ``is_dir`` dance :func:`clio_agent.gact.routes.workspaces.list_workspace_files`
    already performs.
    """

    ws = app.state.workspaces.get(workspace_id)
    if ws is None:
        return None
    root_path = str(getattr(ws, "root_path", "") or "")
    if not root_path:
        return None
    root = Path(root_path).expanduser()
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    return root.resolve(strict=False)


def _watch_filter(root: Path):
    """Build a ``watchfiles`` filter that reuses the listing's own skip rule.

    Never a second, invented ignore list (CLAUDE.md RULE): every path
    component under ``root`` is checked against the exact same
    :func:`skip_workspace_file_directory` predicate the ``@``-picker/Files
    walk applies. ``.clio`` is not in that set, so upload materialization
    into ``.clio/inputs`` is never filtered out here.
    """

    def _filter(_change: Change, changed_path: str) -> bool:
        try:
            parts = Path(changed_path).relative_to(root).parts
        except ValueError:
            # Outside root shouldn't happen (we only ever watch `root`), but
            # an unrecognized shape is not this filter's call to make -- keep it.
            return True
        return not any(skip_workspace_file_directory(part) for part in parts)

    return _filter


def _relative_posix(root: Path, changed_path: str) -> str | None:
    """Return ``changed_path`` as a workspace-relative, forward-slash path.

    ``None`` for anything outside ``root`` or for the root directory itself
    (a bare metadata touch some backends report when a child changes --
    not a "file" a client query is ever keyed on).
    """

    try:
        relative = Path(changed_path).relative_to(root)
    except ValueError:
        return None
    posix = relative.as_posix()
    return posix if posix and posix != "." else None


def _publish_changes(
    app: "FastAPI", workspace_id: str, root: Path, changes: set[tuple[Change, str]]
) -> None:
    """Project one debounced ``watchfiles`` batch into a single bus event."""

    seen: dict[str, None] = {}
    for _change, changed_path in sorted(changes, key=lambda item: item[1]):
        relative = _relative_posix(root, changed_path)
        if relative is None or relative in seen:
            continue
        seen[relative] = None
    if not seen:
        return
    paths = list(seen.keys())
    truncated = len(paths) > MAX_CHANGED_PATHS
    app.state.bus.publish(
        Event(
            type=EVENT_TYPE,
            # Broadcast (session_id="") like lm.provider.changed / lm.provider.failed
            # (routes/providers.py): this is workspace-scoped, not one session's own
            # timeline, and any live subscriber's Files view may care regardless of
            # which session it is currently viewing. The payload carries workspace_id
            # so the client filters.
            session_id="",
            payload={
                "workspace_id": workspace_id,
                "paths": paths[:MAX_CHANGED_PATHS],
                "truncated": truncated,
            },
        )
    )


def _record_unavailable(workspace_id: str, root: Path, exc: BaseException) -> WorkspaceWatchStatus:
    trace.event(
        "WORKSPACE",
        "workspace_watch_unavailable workspace_id=%s root=%s cause=%r",
        workspace_id,
        root,
        exc,
    )
    logger.warning(
        "workspace_watch_unavailable workspace_id=%s root=%s cause=%s",
        workspace_id,
        root,
        exc,
    )
    return WorkspaceWatchStatus(active=False, reason=UNAVAILABLE_REASON, detail=str(exc))


class WorkspaceWatchRegistry:
    """One live-notification watcher per workspace root, refcounted.

    Lifecycle (owner spec): a watcher starts when a workspace gets its first
    live SSE subscriber (:meth:`acquire`) and stops when the last one
    disconnects (:meth:`release`), when the workspace is deleted
    (:meth:`remove`), or at server shutdown (:meth:`shutdown`). Tests must
    drive the real ``TestClient`` context manager so the shutdown path -- and
    therefore this registry's own teardown -- actually runs.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._handles: dict[str, _WatchHandle] = {}

    async def acquire(self, app: "FastAPI", workspace_id: str, root: Path) -> None:
        """Ensure a watcher is running for ``workspace_id``; bump its refcount.

        Awaits the watcher's first setup attempt (success or a typed failure)
        so :meth:`status` is meaningful immediately after this returns --
        never a race where the caller reads "active" before the OS watch call
        has actually been attempted.
        """

        async with self._lock:
            handle = self._handles.get(workspace_id)
            if handle is None:
                handle = _WatchHandle(root=root)
                handle.task = asyncio.create_task(self._run(app, workspace_id, handle))
                self._handles[workspace_id] = handle
            handle.refcount += 1
        await handle.ready_event.wait()

    async def release(self, app: "FastAPI", workspace_id: str) -> None:
        """Drop one reference; stop the watcher once the last subscriber leaves."""

        del app  # unused: symmetry with acquire()'s signature, nothing to publish here
        async with self._lock:
            handle = self._handles.get(workspace_id)
            if handle is None:
                return
            handle.refcount = max(0, handle.refcount - 1)
            if handle.refcount > 0:
                return
            del self._handles[workspace_id]
        await self._stop(handle)

    async def remove(self, workspace_id: str) -> None:
        """Force-stop a workspace's watcher regardless of refcount (workspace deleted)."""

        async with self._lock:
            handle = self._handles.pop(workspace_id, None)
        if handle is not None:
            await self._stop(handle)

    async def shutdown(self) -> None:
        """Stop every live watcher. Called once from the server lifespan teardown."""

        async with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            await self._stop(handle)

    def status(self, workspace_id: str) -> dict[str, Any]:
        """Wire-shaped status for the files-listing response to embed."""

        handle = self._handles.get(workspace_id)
        if handle is None:
            return WorkspaceWatchStatus().to_wire()
        return handle.status.to_wire()

    @staticmethod
    async def _stop(handle: _WatchHandle) -> None:
        handle.stop_event.set()
        task = handle.task
        if task is None:
            return
        with contextlib.suppress(Exception):
            await task

    async def _run(self, app: "FastAPI", workspace_id: str, handle: _WatchHandle) -> None:
        """The resident watch loop: one task per :class:`_WatchHandle`.

        The FIRST ``__anext__()`` is awaited manually (rather than started
        inside ``async for``) so a construction failure (permission denied,
        an unsupported filesystem) is distinguishable from "no changes yet":
        ``watchfiles`` raises synchronously from ``RustNotify`` construction
        on the very first iteration, and ``yield_on_timeout=True`` makes a
        healthy-but-quiet watcher return an empty set well within a second
        rather than blocking -- so either outcome settles ``ready_event``
        quickly and honestly.
        """

        watcher = awatch(
            handle.root,
            watch_filter=_watch_filter(handle.root),
            debounce=DEBOUNCE_MS,
            stop_event=handle.stop_event,
            yield_on_timeout=True,
        )
        try:
            first = await watcher.__anext__()
        except StopAsyncIteration:
            # stop_event was already set before the watcher ever got going
            # (acquire immediately followed by release/remove) -- not a failure.
            handle.status = WorkspaceWatchStatus(active=False)
            handle.ready_event.set()
            return
        except Exception as exc:  # noqa: BLE001 - every backend failure becomes a typed reason, never a crash
            handle.status = _record_unavailable(workspace_id, handle.root, exc)
            handle.ready_event.set()
            return
        handle.status = WorkspaceWatchStatus(active=True)
        handle.ready_event.set()
        if first:
            _publish_changes(app, workspace_id, handle.root, first)
        try:
            async for changes in watcher:
                if changes:
                    _publish_changes(app, workspace_id, handle.root, changes)
        except Exception as exc:  # noqa: BLE001 - a mid-life failure degrades the same typed way as a start failure
            handle.status = _record_unavailable(workspace_id, handle.root, exc)


__all__ = [
    "DEBOUNCE_MS",
    "EVENT_TYPE",
    "MAX_CHANGED_PATHS",
    "UNAVAILABLE_REASON",
    "WorkspaceWatchRegistry",
    "WorkspaceWatchStatus",
    "resolve_workspace_root",
]
