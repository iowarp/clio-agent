"""Whether the managed packages (document tools, Office) are being prepared right now.

A desktop installed from a .deb/.rpm/.dmg has no per-user install step, so its first
launch installs the managed packages (~36 s) while the agent is already starting (B4:
the agent never waits for it). The installer step
(:mod:`clio_agent.runtime.document_install`) records its progress here, and the agent
reads it: a turn that needs documents or Office while they are still being prepared
gets a typed ``managed_packages_preparing`` answer instead of a failure or a second,
racing install, and ``/v1/health`` shows the ``managed_packages`` row.

The record is one small JSON file beside the install receipts. A ``preparing`` record
whose process is gone (a killed installer) reads as ``interrupted``, never as forever
preparing.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from clio_agent import paths

STATE_FILE = "preparation.json"
REASON_PREPARING = "managed_packages_preparing"
REASON_FAILED = "managed_packages_install_failed"
REASON_INTERRUPTED = "managed_packages_preparation_interrupted"


def state_path(cache_root: Path | None = None) -> Path:
    """The preparation record beside the document-runtime install receipts."""
    return (cache_root or paths.user_cache_dir() / "document-runtime") / STATE_FILE


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def _process_started(pid: int) -> float | None:
    import psutil  # noqa: PLC0415 - only needed to judge a recorded installer

    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ValueError):
        return None


@contextmanager
def preparing(cache_root: Path | None = None) -> Iterator[None]:
    """Record a full package preparation from start to its outcome."""
    path = state_path(cache_root)
    started = time.time()
    _write(
        path,
        {
            "state": "preparing",
            "reason": REASON_PREPARING,
            "pid": os.getpid(),
            "process_started": _process_started(os.getpid()),
            "started_at": started,
        },
    )
    try:
        yield
    except BaseException as error:
        _write(
            path,
            {
                "state": "failed",
                "reason": REASON_FAILED,
                "error": f"{type(error).__name__}: {error}"[-2000:],
                "started_at": started,
                "finished_at": time.time(),
            },
        )
        raise
    _write(path, {"state": "ready", "started_at": started, "finished_at": time.time()})


def current(cache_root: Path | None = None) -> dict[str, Any] | None:
    """The last recorded preparation, or ``None`` when none was recorded."""
    try:
        record = json.loads(state_path(cache_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "unreadable", "reason": "managed_packages_state_unreadable"}
    if not isinstance(record, dict):
        return {"state": "unreadable", "reason": "managed_packages_state_unreadable"}
    if record.get("state") == "preparing":
        started = _process_started(int(record.get("pid") or 0))
        recorded = record.get("process_started")
        if started is None or (recorded is not None and abs(started - float(recorded)) > 1.0):
            return {**record, "state": "interrupted", "reason": REASON_INTERRUPTED}
    return record


def preparing_result(cache_root: Path | None = None) -> dict[str, Any] | None:
    """A typed "still preparing" tool answer while an install is running, else ``None``."""
    record = current(cache_root)
    if record is None or record.get("state") != "preparing":
        return None
    elapsed = max(0.0, time.time() - float(record.get("started_at") or time.time()))
    return {
        "status": "preparing",
        "reason": REASON_PREPARING,
        "elapsed_seconds": round(elapsed, 1),
        "message": (
            "Document tools and Office are still being installed for this computer "
            f"(started {elapsed:.0f} s ago; first launch only). Try again in a minute, "
            "or continue with work that does not need them."
        ),
    }


def probe_managed_packages() -> Any:
    """The ``managed_packages`` doctor row (not required: the agent runs without it)."""
    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    record = current()
    state, summary = IntegrationState.SKIPPED, "No package preparation recorded on this computer."
    next_action = "Document tools and Office are prepared on first use or at install."
    if record is not None and record.get("state") == "ready":
        state, summary, next_action = (
            IntegrationState.READY,
            "Document tools and Office were installed.",
            "None.",
        )
    elif record is not None and record.get("state") == "preparing":
        state, summary = IntegrationState.DEGRADED, "Document tools and Office are being installed."
        next_action = "Wait for the installation to finish; other work is unaffected."
    elif record is not None:
        state, summary = IntegrationState.DEGRADED, "The last package installation did not finish."
        next_action = "Restart CLIO to retry, or run 'clio-agent doctor' for details."
    return IntegrationStatus(
        name="managed_packages",
        state=state,
        summary=summary,
        config_source="runtime:document_install",
        next_action=next_action,
        details=dict(record or {}),
        required=False,
    )
