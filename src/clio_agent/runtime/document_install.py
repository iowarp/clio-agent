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
from clio_agent.runtime import package_preparation
from clio_agent.runtime.codex_desktop_setup import prepare_existing_windows_fence
from clio_agent.runtime.codex_windows_helpers import ensure_bundled_codex_windows_helpers
from clio_agent.runtime.document_install_receipt import reuse_install_receipt, save_install_receipt
from clio_agent.runtime.document_runtime import (
    DocumentRuntimeError,
    prepare_document_runtime,
    prepare_office_runtime,
)
from clio_agent.runtime.execution_environment import publish_environment
from clio_agent.runtime.github_cli import ensure_github_cli


def prepare_protected_execution(
    *, setup_protected_execution: bool = False, progress: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """Verify the Windows fence independently of document package preparation."""
    if progress is not None:
        progress("Preparing and checking protected execution...")
    protected = prepare_existing_windows_fence(
        progress=progress, allow_elevation=setup_protected_execution
    )
    helpers = (
        {"status": "not_required", "implementation": "mxc"}
        if protected.get("implementation") == "mxc"
        else ensure_bundled_codex_windows_helpers()
    )
    if progress is not None and protected["status"] == "setup_required":
        progress(protected["next_action"])
    return {"protected_execution": protected, "codex_windows_helpers": helpers}


def prepare_managed_packages(
    workspace: Path,
    *,
    cache_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Prepare package caches without qualifying protection or saving an install receipt."""
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

    with package_preparation.preparing(cache_root):
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
    return result


def install_document_runtime(
    workspace: Path,
    *,
    cache_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
    setup_protected_execution: bool = False,
    reuse_installed: bool = False,
) -> dict[str, Any]:
    """Verify packages and protection before publishing a complete installation receipt."""
    workspace.mkdir(parents=True, exist_ok=True)
    result = (
        reuse_install_receipt(workspace, cache_root=cache_root)
        if reuse_installed and not setup_protected_execution
        else None
    )
    reused = result is not None
    if result is None:
        result = prepare_managed_packages(workspace, cache_root=cache_root, progress=progress)
    else:
        from clio_agent.runtime.document_stack.process import scratch_root
        from clio_agent.tools.file_policy import FileAccessPolicy

        policy = FileAccessPolicy.from_env()
        scratch = scratch_root(workspace, create=False)
        for name in ("output", "javascript"):
            policy.validate_write(str(scratch / name / "manifest.json"), create_parent=True)
        cache = (cache_root or paths.user_cache_dir() / "document-runtime") / result["runtime_id"]
        publish_environment(workspace, result, cache)
        if progress is not None:
            progress(
                "Verified installation unchanged; reusing Office, Python, Node.js and GitHub packages."
            )
    result["native_tools"].update(
        prepare_protected_execution(
            progress=progress, setup_protected_execution=setup_protected_execution
        )
    )
    if not reused:
        save_install_receipt(workspace, result, cache_root=cache_root)
    if progress is not None:
        progress("All managed runtime packages are installed and verified.")
    return result


def prune_after_install() -> dict[str, Any]:
    """Bound the MCP spawn cache at the end of an install or upgrade (B4).

    Pruning is maintenance, never server start. A skip or a failure is reported with
    a typed reason and never fails the install; ``clio-agent doctor --gc`` reclaims
    the same cache on demand.
    """
    from clio_agent.tools.mcp_cache import prune_owned_mcp_cache  # noqa: PLC0415

    try:
        pruned = prune_owned_mcp_cache()
    except OSError as error:
        return {"status": "failed", "reason": "mcp_cache_prune_failed", "error": str(error)}
    if pruned is None:
        return {"status": "skipped", "reason": "mcp_cache_prune_skipped_live"}
    return {"status": "pruned", "freed_bytes": pruned.bytes_freed, "evicted": pruned.count}


def main() -> None:
    """Run the shared installer step; an incomplete setup exits unsuccessfully."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=paths.user_data_dir() / "workspace")
    parser.add_argument(
        "--protected-execution-only",
        action="store_true",
        help="Verify protected execution before starting the agent, without preparing document tools.",
    )
    parser.add_argument(
        "--reuse-installed",
        action="store_true",
        help="Reuse installation checks while the verified managed package files are unchanged.",
    )
    parser.add_argument(
        "--setup-protected-execution",
        action="store_true",
        help="Request one-time Windows sandbox setup with administrator approval if needed.",
    )
    parser.add_argument(
        "--skip-store-migrations",
        action="store_true",
        help="Do not run `clio-agent migrate` here (the caller runs it as its own step).",
    )
    args = parser.parse_args()
    if args.protected_execution_only:
        tools = prepare_protected_execution(
            progress=lambda message: print(message, flush=True),
            setup_protected_execution=args.setup_protected_execution,
        )
        ready = tools["protected_execution"]["status"] in {"available", "not_required"}
        print(json.dumps({"status": "ready" if ready else "setup_required", **tools}), flush=True)
        if not ready:
            raise SystemExit(1)
        return
    # This standalone installer owns its workspace. Never widen a running agent's policy.
    os.environ["CLIO_ALLOWED_ROOTS"] = str(args.workspace.resolve())
    result = install_document_runtime(
        args.workspace,
        progress=lambda message: print(message, flush=True),
        setup_protected_execution=args.setup_protected_execution,
        reuse_installed=args.reuse_installed,
    )
    # The install-time prepare (not the per-start reuse check) also upgrades the store,
    # so the desktop's first start after an install or upgrade only checks the markers.
    migrations = _run_store_migrations(args)
    summary = {
        "status": result["status"],
        "runtime_id": result["runtime_id"],
        "office": result["native_tools"]["soffice"],
        "store_migrations": migrations,
    }
    if not args.reuse_installed:
        # An install or upgrade, not a per-launch check: bound the MCP cache now (B4).
        summary["mcp_cache"] = prune_after_install()
    print(json.dumps(summary), flush=True)


def _run_store_migrations(args: argparse.Namespace) -> dict[str, Any]:
    """``clio-agent migrate`` as this installer's last step, reported typed.

    A refusal or failure does not fail the package install: the server's next boot runs
    an owed migration once and its ``store_migrations`` health row reports the outcome.
    """
    if args.reuse_installed or args.skip_store_migrations:
        reason = "reuse_installed" if args.reuse_installed else "skipped_by_caller"
        return {"status": "skipped", "reason": reason}
    from clio_agent.gact.store_migrate_command import run_migrations  # noqa: PLC0415

    print("Upgrading the CLIO store (clio-agent migrate)...", flush=True)
    _code, payload = run_migrations(lambda line: print(line, flush=True))
    return {key: payload[key] for key in ("status", "reason", "seconds") if key in payload}


if __name__ == "__main__":
    main()
