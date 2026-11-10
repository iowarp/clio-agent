"""Install locked execution packages and a private Office renderer after app installation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    _fingerprint,
    installer_plan_files,
    prepare_document_runtime,
    prepare_office_runtime,
)
from clio_agent.runtime.document_stack.office import INSTALLER_DOWNLOADS, installer_download
from clio_agent.runtime.execution_environment import (
    protect_bundled_runtime,
    publish_environment,
)
from clio_agent.runtime.github_cli import ensure_github_cli
from clio_agent.runtime.ripgrep import ensure_ripgrep
from clio_agent.runtime.tectonic import ensure_tectonic

logger = logging.getLogger(__name__)

#: ``now`` | ``first-launch`` | ``auto``: when the installer step prepares Office and the
#: Python/Node document packages (``--managed-packages`` overrides it).
MANAGED_PACKAGES_ENV = "CLIO_INSTALL_MANAGED_PACKAGES"
REASON_FIRST_LAUNCH = "managed_packages_first_launch"
_KEPT_DOWNLOADS = "installer-downloads"


def _kept_downloads(cache_root: Path | None = None) -> Path:
    return (cache_root or paths.user_cache_dir() / "document-runtime") / _KEPT_DOWNLOADS


def keep_installer_downloads(cache_root: Path | None = None) -> list[str]:
    """Keep the installer's verified prefetched files for the first launch's preparation.

    The installer deletes its download folder when it exits; the files the first launch
    will need (LibreOffice, pnpm) are linked, or copied across volumes, into a per-user
    folder instead of being downloaded a second time. A download still in progress is
    waited for, as the full preparation would.
    """
    if not os.environ.get(INSTALLER_DOWNLOADS):
        return []
    kept: list[str] = []
    folder = _kept_downloads(cache_root)
    for name, checksum in installer_plan_files().items():
        source = installer_download(name, checksum)
        if source is None:
            continue
        target = folder / name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            try:
                os.link(source, target)
            except OSError:
                partial = target.with_name(name + ".part")
                shutil.copyfile(source, partial)
                partial.replace(target)
        except OSError as error:
            logger.warning(
                "reason=installer_download_not_kept %s: %s; the first launch downloads it",
                name,
                error,
            )
            continue
        kept.append(name)
    return kept


@contextmanager
def _using_kept_downloads(cache_root: Path | None) -> Iterator[None]:
    """Let a preparation without installer downloads use the ones an install kept.

    The kept folder is removed once the preparation has succeeded.
    """
    folder = _kept_downloads(cache_root)
    if os.environ.get(INSTALLER_DOWNLOADS) or not folder.is_dir():
        yield
        return
    os.environ[INSTALLER_DOWNLOADS] = str(folder)
    try:
        yield
    finally:
        os.environ.pop(INSTALLER_DOWNLOADS, None)
    shutil.rmtree(folder, ignore_errors=True)


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
    protect_bundled_runtime()
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

    def prepare_ripgrep() -> Path:
        report("Preparing and checking ripgrep for file search...")
        ripgrep = ensure_ripgrep()
        report("ripgrep is ready.")
        return ripgrep

    def prepare_tectonic() -> Path:
        report("Preparing and checking Tectonic for LaTeX builds...")
        tectonic = ensure_tectonic()
        report("Tectonic is ready.")
        return tectonic

    with _using_kept_downloads(cache_root), package_preparation.preparing(cache_root):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="clio-install") as workers:
            office_job = workers.submit(prepare_office)
            github_job = workers.submit(prepare_github)
            ripgrep_job = workers.submit(prepare_ripgrep)
            tectonic_job = workers.submit(prepare_tectonic)
            result = prepare_document_runtime(workspace, cache_root=cache_root, progress=report)
            if result["javascript"]["status"] != "ready":
                raise DocumentRuntimeError(
                    f"Node/pnpm package installation failed: {result['javascript']}"
                )
            office = office_job.result()
            github = github_job.result()
            ripgrep = ripgrep_job.result()
            tectonic = tectonic_job.result()
        result["native_tools"]["soffice"] = {"status": "available", "path": office}
        result["capabilities"]["office_render_recalculate"] = "available"
        result["native_tools"]["gh"] = {
            "status": "available",
            "path": str(github),
            "authentication": "clio_account",
        }
        result["native_tools"]["rg"] = {"status": "available", "path": str(ripgrep)}
        result["native_tools"]["tectonic"] = {"status": "available", "path": str(tectonic)}
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


def defer_managed_packages(
    *,
    setup_protected_execution: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The install step without the managed packages, which the first launch installs.

    Desktop's first launch checks the packages after the agent is ready; finding no
    receipt, it installs them in the background while turns that need them get a typed
    ``managed_packages_preparing`` answer (as on .dmg/.deb installs). Protected
    execution is still set up here, where administrator approval can be asked for.
    """
    protect_bundled_runtime()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-keep") as keeping:
        kept = keeping.submit(keep_installer_downloads)
        protected = prepare_protected_execution(
            progress=progress, setup_protected_execution=setup_protected_execution
        )
        kept_names = kept.result()
    if progress is not None:
        progress(
            "Office and the Python/Node.js document packages are installed on first launch, "
            f"in the background (reason={REASON_FIRST_LAUNCH})."
        )
    return {
        "status": "deferred",
        "runtime_id": _fingerprint(),
        "native_tools": {
            "soffice": {"status": "deferred", "reason": REASON_FIRST_LAUNCH},
            **protected,
        },
        "kept_downloads": kept_names,
    }


def _managed_packages_now(choice: str, *, reuse_installed: bool) -> bool:
    """Whether this installer step prepares the managed packages itself.

    ``auto`` defers them only for Desktop's own install step (its bundled runtime, not
    a per-launch check): a source install or a CI preparation still prepares them now.
    """
    if reuse_installed or choice == "now":
        return True
    if choice == "first-launch":
        return False
    return not os.environ.get("GACT_BUNDLED_RUNTIME_DIR")


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


def collect_snapshots_after_install() -> dict[str, Any]:
    """Remove superseded marketplace snapshots at the end of an install or upgrade.

    Gated like the MCP cache prune: skipped with a typed reason while a peer clio
    process (a running server, which may read a snapshot) is alive; ``clio-agent doctor
    --gc`` collects the same snapshots on demand.
    """
    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415
    from clio_agent.plugins.snapshot_gc import collect_superseded_snapshots  # noqa: PLC0415
    from clio_agent.runtime.disk_gc import live_peer_clio_processes  # noqa: PLC0415

    if list(live_peer_clio_processes()):
        return {"status": "skipped", "reason": "marketplace_snapshots_skipped_live"}
    try:
        collected = collect_superseded_snapshots(plugin_store_root())
    except (OSError, ValueError) as error:
        return {"status": "failed", "reason": "marketplace_snapshot_gc_failed", "error": str(error)}
    return {
        "status": "collected",
        "freed_bytes": collected.bytes_freed,
        "removed": len(collected.removed),
        "kept": collected.kept,
    }


def _report_warnings() -> None:
    """Send CLIO's warnings to stderr, where Desktop and the installers keep them.

    Without a handler Python prints only bare messages of WARNING and above, losing
    their logger and the typed ``reason=`` they carry.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    owner = logging.getLogger("clio_agent")
    owner.addHandler(handler)
    owner.setLevel(logging.WARNING)


def main() -> None:
    """Run the installer step; a failed step exits 1 with its message, not a traceback."""
    _report_warnings()
    try:
        _main()
    except DocumentRuntimeError as error:
        print(f"CLIO runtime setup failed: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def _main() -> None:
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
    parser.add_argument(
        "--managed-packages",
        choices=("auto", "now", "first-launch"),
        default=None,
        help=(
            "When Office and the Python/Node.js document packages are installed: now, or on "
            "first launch in the background. auto (default; also "
            f"{MANAGED_PACKAGES_ENV}) defers them only for Desktop's install step."
        ),
    )
    args = parser.parse_args()
    choice = args.managed_packages or os.environ.get(MANAGED_PACKAGES_ENV, "auto")
    if choice not in {"auto", "now", "first-launch"}:
        parser.error(f"{MANAGED_PACKAGES_ENV} must be auto, now or first-launch, not {choice!r}")
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
    output_lock = Lock()

    def emit(message: str) -> None:
        with output_lock:
            print(message, flush=True)

    # The install-time prepare (not the per-start reuse check) also upgrades the store,
    # so the desktop's first start after an install or upgrade only checks the markers.
    # The store reads none of the packages, so it is upgraded while they are prepared.
    # clio-core is entered from the main thread, as before: from any other thread each
    # native call is handed to a persistent native worker (arc/native_threads.py).
    # Leaving this block waits for the packages too, so neither outlives the other.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-packages") as installing:
        if _managed_packages_now(choice, reuse_installed=args.reuse_installed):
            installation = installing.submit(
                install_document_runtime,
                args.workspace,
                progress=emit,
                setup_protected_execution=args.setup_protected_execution,
                reuse_installed=args.reuse_installed,
            )
        else:
            installation = installing.submit(
                defer_managed_packages,
                progress=emit,
                setup_protected_execution=args.setup_protected_execution,
            )
        migrations = _run_store_migrations(args, emit)
        result = installation.result()
    summary = {
        "status": result["status"],
        "runtime_id": result["runtime_id"],
        "office": result["native_tools"]["soffice"],
        "store_migrations": migrations,
    }
    if not args.reuse_installed:
        # An install or upgrade, not a per-launch check: bound the MCP cache now (B4).
        summary["mcp_cache"] = prune_after_install()
        summary["marketplace_snapshots"] = collect_snapshots_after_install()
    print(json.dumps(summary), flush=True)


def _run_store_migrations(
    args: argparse.Namespace, emit: Callable[[str], None] = print
) -> dict[str, Any]:
    """``clio-agent migrate`` alongside this installer's package step, reported typed.

    A refusal or failure does not fail the package install: the server's next boot runs
    an owed migration once and its ``store_migrations`` health row reports the outcome.
    """
    if args.reuse_installed or args.skip_store_migrations:
        reason = "reuse_installed" if args.reuse_installed else "skipped_by_caller"
        return {"status": "skipped", "reason": reason}
    from clio_agent.gact.store_migrate_command import run_migrations  # noqa: PLC0415

    emit("Upgrading the CLIO store (clio-agent migrate)...")
    _code, payload = run_migrations(emit)
    return {key: payload[key] for key in ("status", "reason", "seconds") if key in payload}


if __name__ == "__main__":
    main()
