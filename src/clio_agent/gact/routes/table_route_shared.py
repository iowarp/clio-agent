"""Helpers shared between the table-query and table-export HTTP routes.

Both serve registered CSV/Parquet artifacts under the same custody,
bound and cancellation rules; this is their one shared owner module, so
neither reaches into the other's private names (#1551 review item 5).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.artifacts.cas import sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.routes.artifact_table_preview import (
    _artifact_source,
    _error,
    _workspace_root,
)

logger = logging.getLogger(__name__)

#: How often the disconnect watcher polls the client connection.
DISCONNECT_POLL_S = 0.25


def import_table_engine_once(*, export: bool = False) -> None:
    """Import the table-query (and, if ``export``, table-export) engine
    (numpy/pyarrow) on the CURRENT thread, at route-registration time -- not
    the first time a request reaches a worker thread (#1551 review item 6).

    numpy/pyarrow themselves are pre-imported even earlier than this, at the
    very top of ``build_app`` (the app factory), specifically so this call
    and every other importer in the process always find them already fully
    initialized -- see that pre-import's own comment for the root-caused
    race (``_construct_agent_async``'s executor thread importing ``dspy``,
    which transitively imports numpy) this once worked around with a
    retry-and-purge of ``sys.modules`` instead of fixing. That retry is
    deleted: purging and re-importing numpy mid-process does not give a
    clean module -- numpy's C extension does not support being initialized
    twice in one process, so a purge-and-retry can leave two incompatible
    numpy objects alive in different already-imported modules, which is
    worse than the original failure and silent about it. A failure here now
    surfaces as a typed startup error, not a swallowed warning.
    """

    import importlib  # noqa: PLC0415

    importlib.import_module("clio_agent.gact.artifacts.table_query")
    if export:
        importlib.import_module("clio_agent.gact.artifacts.table_export")


def owned_error(
    *, status_code: int, error: str, message: str, details: dict[str, Any] | None = None
) -> HTTPException:
    """Adapt the storage-owner error seam onto the table-query/export error envelope."""

    return _error(status_code, error, message, **(details or {}))


def table_source(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
    *,
    verify: bool = True,
) -> Path:
    """Resolve bytes exactly as ``/bytes`` does: owned store first, then CAS/path.

    ``verify`` (default ``True``) re-hashes the resolved bytes against
    ``version.sha256``, exactly like every table-query/table-preview/
    table-export serving path. ``verify=False`` trusts the artifact record's
    own recorded metadata instead of re-reading and re-hashing the whole
    file -- used by producer-side, non-serving checks that run on every
    ``create_a2ui_surface``/``update_a2ui_components`` call and must not pay
    a full artifact re-hash each time just to check column names.
    """

    from clio_agent.gact.artifacts.storage import (  # noqa: PLC0415
        resolve_owned_artifact_or_raise,
    )

    root = _workspace_root(app, record.workspace_id)
    owned = resolve_owned_artifact_or_raise(app, version, workspace_root=root, error=owned_error)
    if owned is None:
        return _artifact_source(app, record, version, verify=verify)
    if verify and version.sha256:
        actual = sha256_file(owned)
        if actual != version.sha256:
            raise _error(
                409,
                "integrity_violation",
                "artifact store bytes do not match the immutable version hash",
                artifact_id=version.artifact_id,
                recorded_sha256=version.sha256,
                actual_sha256=actual,
            )
    return owned


async def watch_for_disconnect(request: Request, cancel_event: threading.Event) -> None:
    """Set ``cancel_event`` as soon as ``request``'s own HTTP client disconnects.

    The PRIMARY cancellation path (owner ruling): polled rather than a single
    ``await``, since Starlette's ``is_disconnected`` only reports a truthful
    answer when asked repeatedly. Cancelled by the route once the query/export
    finishes (success or error) either way -- this task never outlives one
    request.
    """

    while True:
        if await request.is_disconnected():
            cancel_event.set()
            return
        await asyncio.sleep(DISCONNECT_POLL_S)


def report_watcher_failure(task: "asyncio.Task[None]") -> None:
    """Log an unexpected ``watch_for_disconnect`` failure; never re-raise it.

    The route cancels the watcher fire-and-forget, so this done-callback is
    the only place a bug in the watcher itself would otherwise surface --
    without it, a non-cancellation exception here would just vanish into
    "Task exception was never retrieved" at GC time instead of a readable
    log line (no silent fallback). Cancellation is the expected, silent
    outcome.
    """

    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("table route disconnect watcher failed: %r", exc)


__all__ = [
    "DISCONNECT_POLL_S",
    "import_table_engine_once",
    "owned_error",
    "report_watcher_failure",
    "table_source",
    "watch_for_disconnect",
]
