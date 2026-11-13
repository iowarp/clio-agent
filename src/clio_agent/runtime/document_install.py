"""Install locked execution packages and a private Office renderer after app installation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from clio_agent import paths
from clio_agent.runtime import package_preparation
from clio_agent.runtime.codex_desktop_setup import prepare_existing_windows_fence
from clio_agent.runtime.codex_windows_helpers import ensure_bundled_codex_windows_helpers
from clio_agent.runtime.document_install_receipt import (
    reuse_install_receipt,
    save_install_receipt,
    take_prepared_receipt,
)
from clio_agent.runtime.document_runtime import (
    DocumentRuntimeError,
    _document_uv,
    prepare_document_runtime,
    prepare_office_runtime,
)
from clio_agent.runtime.document_stack.process import end_on_termination
from clio_agent.runtime.execution_environment import (
    protect_bundled_runtime,
    publish_environment,
)
from clio_agent.runtime.github_cli import ensure_github_cli
from clio_agent.runtime.install_tool_servers import KIT_READY_ENV, _prepare_tool_servers
from clio_agent.runtime.ripgrep import ensure_ripgrep
from clio_agent.runtime.tectonic import ensure_tectonic


def _timed[T](timings: dict[str, float] | None, key: str, work: Callable[[], T]) -> T:
    """Run ``work`` and record its seconds under ``key`` when timings are collected."""
    started = time.monotonic()
    try:
        return work()
    finally:
        if timings is not None:
            timings[key] = round(time.monotonic() - started, 1)


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
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Prepare package caches without qualifying protection or saving an install receipt.

    ``timings``, when given, receives each sub-job's seconds (the install reports them).
    """
    _timed(timings, "protect_runtime", protect_bundled_runtime)
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
        office = prepare_office_runtime(timings=timings)
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

    with package_preparation.preparing(cache_root):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="clio-install") as workers:
            office_job = workers.submit(_timed, timings, "office", prepare_office)
            github_job = workers.submit(_timed, timings, "gh", prepare_github)
            ripgrep_job = workers.submit(_timed, timings, "rg", prepare_ripgrep)
            tectonic_job = workers.submit(_timed, timings, "tectonic", prepare_tectonic)
            result = _timed(
                timings,
                "packages",
                lambda: prepare_document_runtime(
                    workspace, cache_root=cache_root, progress=report, timings=timings
                ),
            )
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
    reuse_prepared: bool = False,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Verify packages and protection before publishing a complete installation receipt."""
    workspace.mkdir(parents=True, exist_ok=True)
    result = (
        reuse_install_receipt(workspace, cache_root=cache_root)
        if reuse_installed and not setup_protected_execution
        else None
    )
    reused = result is not None
    protection: dict[str, Any] | None = None
    if result is None and reuse_prepared:
        result = _take_prepared(workspace, cache_root=cache_root, progress=progress)
    if result is None:
        # The fence check touches only the bundled runtime, tool and temp roots, never
        # the package caches, so it runs while the packages are prepared.
        progress_lock = Lock()

        def report(message: str) -> None:
            if progress is not None:
                with progress_lock:
                    progress(message)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-protection") as checking:
            protection_job = checking.submit(
                _timed,
                timings,
                "protection",
                lambda: prepare_protected_execution(
                    progress=report,
                    setup_protected_execution=setup_protected_execution,
                ),
            )
            result = prepare_managed_packages(
                workspace, cache_root=cache_root, progress=report, timings=timings
            )
            protection = protection_job.result()
    else:
        from clio_agent.runtime.document_stack.process import scratch_root
        from clio_agent.tools.file_policy import FileAccessPolicy

        policy = FileAccessPolicy.from_env()
        scratch = scratch_root(workspace, create=False)
        for name in ("output", "javascript"):
            policy.validate_write(str(scratch / name / "manifest.json"), create_parent=True)
        cache = (cache_root or paths.user_cache_dir() / "document-runtime") / result["runtime_id"]
        publish_environment(workspace, result, cache)
        if progress is not None and reused:
            progress(
                "Verified installation unchanged; reusing Office, Python, Node.js and GitHub packages."
            )
    if protection is None:
        protection = prepare_protected_execution(
            progress=progress, setup_protected_execution=setup_protected_execution
        )
    result["native_tools"].update(protection)
    if not reused:
        _timed(
            timings,
            "receipt",
            lambda: save_install_receipt(workspace, result, cache_root=cache_root),
        )
    if progress is not None:
        progress("All managed runtime packages are installed and verified.")
    return result


def _take_prepared(
    workspace: Path,
    *,
    cache_root: Path | None,
    progress: Callable[[str], None] | None,
) -> dict[str, Any] | None:
    """The packages the source installer's side worker prepared, if they are unchanged.

    That worker (:mod:`clio_agent.runtime.office_bootstrap`) ran the same
    :func:`prepare_managed_packages` moments ago; preparing them again here only repeated
    its import probe. Any changed file, or a different uv, prepares them in full.
    """
    protect_bundled_runtime()
    result = take_prepared_receipt(workspace, cache_root=cache_root)
    if result is not None and result.get("uv") != _document_uv():
        result = None
    if progress is not None:
        progress(
            "Verified the packages prepared beside the agent dependencies; reusing them."
            if result is not None
            else "Packages prepared beside the agent dependencies changed; preparing them again."
        )
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


def collect_snapshots_after_install() -> dict[str, Any]:
    """Remove superseded marketplace snapshots at the end of an install or upgrade.

    Gated like the MCP cache prune: skipped with a typed reason while a peer clio
    process (a running server, which may read a snapshot) is alive; ``clio-agent doctor
    --gc`` collects the same snapshots on demand.
    """
    from clio_agent.plugins.service import plugin_store_root  # noqa: PLC0415
    from clio_agent.plugins.snapshot_gc import collect_superseded_snapshots  # noqa: PLC0415
    from clio_agent.runtime.disk_gc import live_peer_clio_processes  # noqa: PLC0415

    if list(live_peer_clio_processes(sharing="config")):
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
    # The Office download also names the mirror that served it and its rate, so a slow
    # install shows where the time went.
    logging.getLogger("clio_agent.runtime.document_stack.office_fetch").setLevel(logging.INFO)


def main() -> None:
    """Run the installer step; a failed step exits 1 with its message, not a traceback."""
    end_on_termination()
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
        "--reuse-prepared",
        action="store_true",
        help="Reuse the packages the Windows source installer prepared beside its sync.",
    )
    parser.add_argument(
        "--prefetch-only",
        action="store_true",
        help="Only fetch the browser and the default agents (the Windows source installer).",
    )
    parser.add_argument(
        "--provider-support",
        default="",
        metavar="KINDS",
        help="Comma-separated provider kinds (e.g. claude_code) whose SDK to install now.",
    )
    parser.add_argument(
        "--prepare-tool-servers",
        action="store_true",
        help=(
            "Build the environments of the clio-kit tool servers the installed agents carry. "
            f"Waits for the installer's clio-kit when {KIT_READY_ENV} names its signal file."
        ),
    )
    args = parser.parse_args()
    if args.prefetch_only:
        # The full step that follows finds all three ready and only checks them.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="clio-prefetch") as fetching:
            browser = fetching.submit(_prepare_browser, args, print)
            marketplace = fetching.submit(_prepare_default_marketplace, args, print)
            providers = fetching.submit(_prepare_provider_support, args, print)
            print(
                json.dumps(
                    {
                        "browser": browser.result(),
                        "marketplace": marketplace.result(),
                        "provider_support": providers.result(),
                    }
                ),
                flush=True,
            )
        return
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
    # The browser's Node.js and Chromium are downloads, not disk work, so they start first
    # and overlap the whole package step instead of waiting for the first browser use.
    # The default marketplace is a fetch too: the server is not ready until it is
    # installed, so a first start that had to fetch it held the workspace for its length.
    # Opening the server's code once here moves the on-access scan of the files the
    # install just wrote out of the first start (see _prewarm_runtime_files).
    # A provider SDK the caller names is a download too; without it the first turn
    # with that provider would install it.
    # The agents' clio-kit tool servers each build an environment on first use (minutes
    # for a cold cache), so an install that asks for them builds them here, once the
    # marketplace that declares them is installed.
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="clio-packages") as installing:
        browser = installing.submit(_prepare_browser, args, emit)
        marketplace = installing.submit(_prepare_default_marketplace, args, emit)
        tool_servers = installing.submit(_prepare_tool_servers, args, marketplace, emit)
        providers = installing.submit(_prepare_provider_support, args, emit)
        prewarm = installing.submit(_prewarm_runtime_files, args)
        installation_timings: dict[str, float] = {}
        installation = installing.submit(
            _timed,
            installation_timings,
            "seconds",
            lambda: install_document_runtime(
                args.workspace,
                progress=emit,
                setup_protected_execution=args.setup_protected_execution,
                reuse_installed=args.reuse_installed,
                reuse_prepared=args.reuse_prepared,
                timings=installation_timings,
            ),
        )
        migrations = _run_store_migrations(args, emit)
        marketplace_result = marketplace.result()
        providers_result = providers.result()
        # The MCP cache prune must not run while the tool servers' builds use that cache.
        tool_servers_result = tool_servers.result()
        maintenance: dict[str, Any] = {}
        if not args.reuse_installed:
            # An install or upgrade, not a per-launch check: bound the MCP cache (B4) and
            # collect superseded snapshots once the marketplace step has written its own,
            # while the packages are still prepared (each scan of the process table took
            # 0.4-1.4 s after them).
            maintenance["mcp_cache"] = prune_after_install()
            maintenance["marketplace_snapshots"] = collect_snapshots_after_install()
        result = installation.result()
        browser_result = browser.result()
        prewarm_result = prewarm.result()
    summary = {
        "status": result["status"],
        "runtime_id": result["runtime_id"],
        "office": result["native_tools"]["soffice"],
        "installation": installation_timings,
        "store_migrations": migrations,
        "browser": browser_result,
        "marketplace": marketplace_result,
        "tool_servers": tool_servers_result,
        "provider_support": providers_result,
        "prewarm": prewarm_result,
        **maintenance,
    }
    print(json.dumps(summary), flush=True)


def _prepare_browser(args: argparse.Namespace, emit: Callable[[str], None]) -> dict[str, Any]:
    """The browser's Node.js driver and Chromium, fetched during an install, reported typed.

    A per-launch check (``--reuse-installed``) never downloads; a failure here does not
    fail the install, and the first browser use retries it.
    """
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    import time  # noqa: PLC0415

    from clio_agent.gact.browser.install import prepare_at_install  # noqa: PLC0415

    emit("Downloading the browser for web pages (Chromium)...")
    started = time.monotonic()
    outcome = prepare_at_install()
    outcome["seconds"] = round(time.monotonic() - started, 1)
    if outcome["status"] == "ready":
        emit("The browser is ready.")
    elif outcome["status"] == "failed":
        emit(f"The browser download failed; its first use will retry: {outcome['error']}")
    return outcome


def _prepare_default_marketplace(
    args: argparse.Namespace, emit: Callable[[str], None]
) -> dict[str, Any]:
    """Install the shipped default marketplace during an install, reported typed.

    The server's start does the same (``PluginService.ready``) and finds it current, so
    the first start no longer waits on a git fetch. A per-launch check never fetches; a
    failure does not fail the install, and the server's start retries it.
    """
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    import time  # noqa: PLC0415

    from clio_agent.plugins.service import PluginService  # noqa: PLC0415

    emit("Installing the default agents (marketplace)...")
    started = time.monotonic()
    service = PluginService()
    service.ready()
    seconds = round(time.monotonic() - started, 1)
    if service.ready_error:
        emit(
            "The default agents were not installed; the first start will retry: "
            f"{service.ready_error.get('message', '')}"
        )
        return {"status": "failed", **service.ready_error, "seconds": seconds}
    emit("The default agents are installed.")
    return {"status": "ready", "seconds": seconds}


_PREWARM_SUFFIXES = (".pyd", ".dll", ".so", ".dylib", ".zip")
# The files a server start opens, recorded by scripts/record_server_imports.py: one path
# per line, relative to the import-path folder that holds it, with "/" separators.
SERVER_IMPORTS = Path(__file__).with_name("server_imports.txt")


def _server_import_files(import_path: list[str]) -> list[str] | None:
    """The files of ``SERVER_IMPORTS`` found on ``import_path``, or ``None`` without it.

    Each line is looked up in the most specific import-path folder first. A ``.py``
    module brings its compiled ``.pyc`` (the checked-hash bytecode is read with its
    source). Lines that match nothing here (another platform's native libraries, a
    module this install lacks) are skipped.
    """
    try:
        lines = SERVER_IMPORTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    entries = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if not entries:
        return None
    suffix = f".{sys.implementation.cache_tag}.pyc"
    folders = sorted({p for p in import_path if os.path.isdir(p)}, key=len, reverse=True)
    files: list[str] = []
    for entry in entries:
        parts = entry.split("/")
        for folder in folders:
            path = os.path.join(folder, *parts)
            if not os.path.isfile(path):
                continue
            files.append(path)
            if path.endswith(".py"):
                stem = os.path.splitext(parts[-1])[0]
                compiled = os.path.join(os.path.dirname(path), "__pycache__", stem + suffix)
                if os.path.isfile(compiled):
                    files.append(compiled)
            break
    return files


def _prepare_provider_support(
    args: argparse.Namespace, emit: Callable[[str], None]
) -> dict[str, Any]:
    """Install the SDK of each provider named by ``--provider-support``, reported typed.

    The same installer the first turn with that provider would run, so the turn finds
    the SDK ready. A per-launch check never downloads; a failure does not fail the
    install, and the first turn with that provider retries it.
    """
    kinds = [kind.strip() for kind in args.provider_support.split(",") if kind.strip()]
    if not kinds:
        return {"status": "skipped", "reason": "none_requested"}
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    import time  # noqa: PLC0415

    from clio_agent.providers.dependencies import (  # noqa: PLC0415
        PROVIDER_SUPPORT,
        ProviderDependencyInstallError,
        ensure_provider_support,
    )

    outcomes: dict[str, Any] = {}
    for kind in kinds:
        support = PROVIDER_SUPPORT.get(kind)
        name = support.display_name if support else kind
        started = time.monotonic()
        try:
            installed = ensure_provider_support(kind)
        except ProviderDependencyInstallError as exc:
            seconds = round(time.monotonic() - started, 1)
            emit(f"{name} support was not installed; its first use will retry: {exc}")
            outcomes[kind] = {"status": "failed", "error": str(exc), "seconds": seconds}
            continue
        seconds = round(time.monotonic() - started, 1)
        emit(f"{name} support is ready.")
        outcomes[kind] = {"status": "ready", "installed": installed, "seconds": seconds}
    failed = any(outcome["status"] == "failed" for outcome in outcomes.values())
    return {"status": "failed" if failed else "ready", "providers": outcomes}


def _runtime_code_files() -> list[str]:
    """The files a server start opens: ``SERVER_IMPORTS``, or else the whole runtime.

    Opening every module of a fully compiled environment (about 31,000 files for a
    source install laid from its prebuilt dependencies) takes far longer than the
    server's own import set (about 5,000), so the recorded set is used when it ships.
    Without it the runtime's shipped bytecode, each module's source and its native
    libraries are opened. The shipped ``.pyc`` files are checked hash-based, so an
    import opens both the bytecode and its source. The roots are this interpreter's
    import path, outermost first, so a folder inside another (site-packages inside
    Lib) is walked once. Only entries inside the interpreter's own prefixes count:
    ``python -m`` puts the caller's working directory first on the path, and an
    installer run from a home directory must not walk the whole home.
    """
    import_path = [os.path.abspath(p) for p in sys.path if p]
    listed = _server_import_files(import_path)
    if listed is not None:
        return [p for p in import_path if os.path.isfile(p)] + listed
    tag = sys.implementation.cache_tag
    suffix = f".{tag}.pyc"
    prefixes = {
        os.path.normcase(os.path.realpath(p))
        for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)
        if p
    }

    def _inside_runtime(entry: str) -> bool:
        real = os.path.normcase(os.path.realpath(entry))
        return any(real == p or real.startswith(p.rstrip(os.sep) + os.sep) for p in prefixes)

    roots: list[str] = []
    files: list[str] = []
    for entry in sorted({os.path.abspath(p) for p in sys.path if p}, key=len):
        if not _inside_runtime(entry):
            continue
        if os.path.isfile(entry):
            files.append(entry)
        elif os.path.isdir(entry) and not any(entry.startswith(root + os.sep) for root in roots):
            roots.append(entry)
    for root in roots:
        for folder, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            if os.path.basename(folder) == "__pycache__":
                parent = os.path.dirname(folder)
                for name in names:
                    if name.endswith(suffix):
                        files.append(os.path.join(folder, name))
                        files.append(os.path.join(parent, name[: -len(suffix)] + ".py"))
                continue
            files.extend(os.path.join(folder, n) for n in names if n.endswith(_PREWARM_SUFFIXES))
    return files


def _open_once(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _prewarm_runtime_files(args: argparse.Namespace) -> dict[str, Any]:
    """Open the runtime's code files once, in parallel, during an install.

    An on-access scanner (Windows Defender) scans a file the installer just wrote the
    first time anything opens it. The server's first start opened about 5,000 of them
    one import at a time: on Windows its import took 33 s cold and 7 s once they were
    opened. A per-launch check skips this; a file that cannot be opened is only counted.
    """
    if args.reuse_installed:
        return {"status": "skipped", "reason": "reuse_installed"}
    import time  # noqa: PLC0415

    started = time.monotonic()
    files = _runtime_code_files()
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="clio-prewarm") as opening:
        opened = sum(opening.map(_open_once, files))
    return {
        "status": "done",
        "files": opened,
        "unreadable": len(files) - opened,
        "seconds": round(time.monotonic() - started, 1),
    }


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


def _exit_released() -> None:
    """Run :func:`main`, then exit without the interpreter's module teardown.

    Every pool is joined and the store is released (``clio-agent migrate`` closes it)
    before :func:`main` returns; the exit handlers still run. The teardown skipped here,
    a garbage collection over everything imported, held the installer that waits for
    this process for seconds after its result line.
    """
    import atexit  # noqa: PLC0415

    code = 0
    try:
        main()
    except SystemExit as stop:
        if stop.code is None or isinstance(stop.code, int):
            code = stop.code or 0
        else:
            print(stop.code, file=sys.stderr)
            code = 1
    atexit._run_exitfuncs()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    _exit_released()
