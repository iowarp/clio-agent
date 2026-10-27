"""``clio-agent migrate``: run every owed store migration once, offline (R2).

The installers (``install/install.sh``, ``install/install.ps1``) and the desktop's
install-time prepare step (:mod:`clio_agent.runtime.document_install`) run this after
installing or upgrading, so the server's boot only checks the markers
(:mod:`clio_agent.gact.store_migrations`).

It opens the same store the server opens (the agent home's clio-core store and session
registry) without serving anything: no restart reconciliation, no agent, no obligations.
Each migration prints one typed line as it starts and one as it ends; ``--json`` prints
the whole result as one object instead. A second run is a no-op (every line reads
``already_done``).

Exit status: 0 when nothing is owed afterwards (or the platform runs without clio-core),
1 when a migration failed, 3 when the store is in use by a running CLIO
(``store_in_use``: stop it, or start it and its boot runs the owed migrations once),
4 when clio-core could not be opened (``store_unavailable``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from clio_agent.arc.init_degradation import ArcStoreUnavailableError
from clio_agent.errors import ClioError
from clio_agent.gact import store_migrations
from clio_agent.gact.store_migrations import MigrationResult, run_migration

EXIT_OK, EXIT_FAILED, EXIT_IN_USE, EXIT_UNAVAILABLE = 0, 1, 3, 4
#: Failures a migration reports typed (``migration_failed``); anything else is a bug and
#: propagates with its traceback (the store is still released).
MIGRATION_ERRORS = (ClioError, OSError, RuntimeError, ValueError, TimeoutError)


def _other_store_clients() -> list[int]:
    """Live processes attached to this home's clio-core runtime, other than this one."""
    from clio_agent.arc import storage  # noqa: PLC0415

    return [pid for pid in storage._live_client_pids() if pid != os.getpid()]


def _open_store() -> Any:
    """Build the app state the migrations read, attached to clio-core, without booting."""
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.arc.memory import ARCMemory  # noqa: PLC0415
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415
    from clio_agent.gact.app import build_app  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import _set_app_arc  # noqa: PLC0415
    from clio_agent.gact.task_history import TaskHistory  # noqa: PLC0415

    app = build_app()
    # Not a server start: the restart reconciliation and metrics seed the boot attaches
    # to the ARC do not run here; only the migrations below touch the store.
    app.state.transcript_boot_ready.set()
    data_dir = str(paths.arc_data_dir())
    arc = ARCMemory(data_dir=data_dir, cache_capacity=1000, store=make_arc_store(data_dir=data_dir))
    _set_app_arc(app, arc)
    history = TaskHistory(arc)
    app.state.task_history = history
    history.io.submit(history.restore).result()
    app.state.human_principal = "human:" + str(app.state.connected_storage.store.clio_id)
    return app


def _close_store(app: Any) -> None:
    """Retire the Core writer and release clio-core (stopped if this was its last client)."""
    from clio_agent.arc.storage import release_runtime_client  # noqa: PLC0415
    from clio_agent.gact import provenance_wiring  # noqa: PLC0415

    history = getattr(app.state, "task_history", None)
    if history is not None:
        asyncio.run(history.io.close())
    close_trace = getattr(getattr(app.state, "semantic_trace_backend", None), "close", None)
    if callable(close_trace):
        close_trace()
    provenance_wiring.close_artifact_backend(app)
    release_runtime_client()


def run_migrations(emit: Callable[[str], None] = print) -> tuple[int, dict[str, Any]]:
    """Run every owed migration in order; return the exit status and the typed result."""
    from clio_agent.arc import history_mode  # noqa: PLC0415

    started = time.monotonic()
    mode = history_mode.resolve()
    if mode.is_history:
        emit(f"migrate status=skipped reason=history_mode detail={mode.reason}")
        return EXIT_OK, {"status": "skipped", "reason": "history_mode", "migrations": []}
    others = _other_store_clients()
    if others:
        emit(
            f"migrate status=refused reason=store_in_use pids={others}: stop CLIO (`clio stop`) "
            "and run `clio-agent migrate` again, or start CLIO: its boot runs owed migrations once"
        )
        return EXIT_IN_USE, {"status": "refused", "reason": "store_in_use", "pids": others}
    emit("migrate step=open_store status=running")
    try:
        app = _open_store()
    except ArcStoreUnavailableError as exc:  # typed store_unavailable outcome
        emit(f"migrate step=open_store status=failed reason=store_unavailable error={exc!r}")
        return EXIT_UNAVAILABLE, {
            "status": "failed",
            "reason": "store_unavailable",
            "error": repr(exc),
        }
    emit(f"migrate step=open_store status=done seconds={time.monotonic() - started:.3f}")
    results: list[MigrationResult] = []
    try:
        history = app.state.task_history
        for item in store_migrations.MIGRATIONS:
            emit(f"migrate step={item.name} status=checking summary={item.summary!r}")
            try:
                result = history.io.submit(run_migration, app, item).result()
            except MIGRATION_ERRORS as exc:  # typed migration_failed; later steps wait
                result = MigrationResult(item.name, "failed", reason="migration_failed")
                result.details = {"error": repr(exc)}
            results.append(result)
            detail = " ".join(f"{key}={value}" for key, value in result.details.items())
            emit(
                f"migrate step={item.name} status={result.status} seconds={result.seconds:.3f}"
                + (f" reason={result.reason}" if result.reason else "")
                + (f" {detail}" if detail else "")
            )
            if result.status == "failed":
                break  # later migrations may read what this one writes
    finally:
        _close_store(app)
    failed = any(result.status == "failed" for result in results)
    seconds = round(time.monotonic() - started, 3)
    status = "failed" if failed else "done"
    ran = sum(result.status == "done" for result in results)
    emit(f"migrate status={status} ran={ran} seconds={seconds:.3f}")
    payload = {
        "status": status,
        "seconds": seconds,
        "migrations": [result.as_dict() for result in results],
    }
    return (EXIT_FAILED if failed else EXIT_OK), payload


def main(arguments: Sequence[str] = ()) -> int:
    """Entry point of ``clio-agent migrate``."""
    parser = argparse.ArgumentParser(
        prog="clio-agent migrate",
        description="Run every owed store migration once (installs and upgrades run this).",
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON result object.")
    args = parser.parse_args(list(arguments))
    from clio_agent.config import load_project_env_file  # noqa: PLC0415

    load_project_env_file()
    stream = sys.stderr if args.json else sys.stdout  # --json: progress on stderr
    emit = lambda line: print(line, file=stream, flush=True)  # noqa: E731
    code, payload = run_migrations(emit)
    if args.json:
        print(json.dumps(payload), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
