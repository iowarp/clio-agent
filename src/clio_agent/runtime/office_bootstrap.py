"""Prepare managed tool caches while the source install resolves agent packages."""

from __future__ import annotations

import json
import os
import time

from clio_agent import paths
from clio_agent.runtime.document_install import prepare_managed_packages
from clio_agent.runtime.document_install_receipt import save_prepared_receipt


def main() -> None:
    """Warm real package caches; the main installer still verifies protection and packages."""
    started = time.monotonic()
    workspace = paths.user_data_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["CLIO_ALLOWED_ROOTS"] = str(workspace.resolve())
    print("Preparing managed tools alongside agent dependencies...", flush=True)
    timings: dict[str, float] = {}
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
