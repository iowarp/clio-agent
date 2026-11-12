"""Prepare managed tool caches while the source install resolves agent packages."""

from __future__ import annotations

import json
import os
import time

from clio_agent import paths
from clio_agent.runtime.document_install import prepare_managed_packages
from clio_agent.runtime.document_install_receipt import save_prepared_receipt


def _setup_timings() -> dict[str, float]:
    """The worker's bootstrap sync and this interpreter's start, before any sub-job."""
    timings: dict[str, float] = {}
    try:
        timings["bootstrap_sync"] = float(os.environ.get("CLIO_BOOTSTRAP_SYNC_SECONDS", ""))
    except ValueError:
        pass
    try:
        import psutil
    except ImportError:
        return timings
    try:
        started = psutil.Process().create_time()
    except (OSError, psutil.Error):
        return timings
    timings["interpreter_start"] = round(max(0.0, time.time() - started), 1)
    return timings


def main() -> None:
    """Warm real package caches; the main installer still verifies protection and packages."""
    timings = _setup_timings()
    started = time.monotonic()
    workspace = paths.user_data_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["CLIO_ALLOWED_ROOTS"] = str(workspace.resolve())
    print("Preparing managed tools alongside agent dependencies...", flush=True)
    result = prepare_managed_packages(
        workspace, progress=lambda line: print(line, flush=True), timings=timings
    )
    # The main step (document_install --reuse-prepared) checks these files, not imports.
    save_prepared_receipt(workspace, result)
    print(
        json.dumps(
            {
                "status": "packages_prepared",
                "runtime_id": result["runtime_id"],
                "seconds": round(time.monotonic() - started, 3),
                "timings": timings,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
