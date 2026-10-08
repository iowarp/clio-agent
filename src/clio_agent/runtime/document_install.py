"""Install locked execution packages and a private Office renderer after app installation."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from clio_agent import paths
from clio_agent.runtime.document_runtime import (
    DocumentRuntimeError,
    prepare_document_runtime,
    prepare_office_runtime,
)
from clio_agent.runtime.github_cli import ensure_github_cli


def install_document_runtime(
    workspace: Path,
    *,
    cache_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Provision and verify all required packages without enlarging the installer payload."""
    workspace.mkdir(parents=True, exist_ok=True)
    # These installers own separate cache trees and locks. Overlap the native
    # downloads/extraction with Python/Node preparation, without changing the
    # package setup's internal ordering or publishing an incomplete receipt.
    progress_lock = Lock()

    def report(message: str) -> None:
        if progress is not None:
            with progress_lock:
                progress(message)

    def prepare_office() -> str:
        report("Preparing and checking the Office renderer...")
        office = prepare_office_runtime()
        report("Office rendering is ready.")
        return office

    def prepare_github() -> Path:
        report("Preparing and checking the GitHub command-line tool...")
        github = ensure_github_cli()
        report("GitHub command-line tool is ready.")
        return github

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="clio-install") as workers:
        office_job = workers.submit(prepare_office)
        github_job = workers.submit(prepare_github)
        result = prepare_document_runtime(workspace, cache_root=cache_root, progress=report)
        if result["javascript"]["status"] != "ready":
            raise DocumentRuntimeError(
                f"Node/pnpm package installation failed: {result['javascript']}"
            )
        office = office_job.result()
        github = github_job.result()
    result["native_tools"]["soffice"] = {"status": "available", "path": office}
    result["capabilities"]["office_render_recalculate"] = "available"
    result["native_tools"]["gh"] = {
        "status": "available",
        "path": str(github),
        "authentication": "clio_account",
    }
    receipt = (
        (cache_root or paths.user_cache_dir() / "document-runtime")
        / result["runtime_id"]
        / "installed.json"
    )
    receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    report("All managed runtime packages are installed and verified.")
    return result


def main() -> None:
    """Run the shared installer step; an incomplete setup exits unsuccessfully."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=paths.user_data_dir() / "workspace")
    args = parser.parse_args()
    # This standalone installer owns its workspace. Never widen a running agent's policy.
    os.environ["CLIO_ALLOWED_ROOTS"] = str(args.workspace.resolve())
    result = install_document_runtime(
        args.workspace, progress=lambda message: print(message, flush=True)
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "runtime_id": result["runtime_id"],
                "office": result["native_tools"]["soffice"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
