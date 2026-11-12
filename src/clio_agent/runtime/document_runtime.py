"""Prepare Clio's isolated, locked document stack on the execution host."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths
from clio_agent.providers.dependencies import _uv_executable
from clio_agent.runtime.document_stack.node_paths import node_path
from clio_agent.runtime.document_stack.npm_package import unpack_package as _unpack_package
from clio_agent.runtime.document_stack.process import scratch_root
from clio_agent.runtime.document_venv import (
    _built_for_this_interpreter,
    _set_environment_home,
    _uv_cache_dir,
)
from clio_agent.runtime.document_wheels import bundled_wheel_requirements
from clio_agent.runtime.execution_environment import (
    bundled_root,
    publish_environment,
)

STACK_ROOT = Path(__file__).resolve().parent / "document_stack"
PNPM_VERSION = "12.9.1"
# pnpm 12 is a JavaScript wrapper that runs a native binary from its per-host
# `@pnpm/exe.*` package. These are the SHA-256 digests of their registry tarballs,
# so the installer's concurrent download of them can stand in for `npm install`
# (install.sh and install.ps1 list them in the release's install plan).
PNPM_PACKAGES = {
    "pnpm": "44c80447645c2a1d8da9308d3efedcec6ca2db4579183abcb15d8a90a88277d4",
    "@pnpm/exe.darwin-arm64": "456316d4cfa8b50b86224613f7f4eca591d970b07ab2f3b6d479724460845d94",
    "@pnpm/exe.linux-x64": "9581c9b2571491f9e00c841cb29864cc3bf96e66f82d55ef5bad70f8f06d1808",
    "@pnpm/exe.linux-arm64": "90530afe00c62fefd1f09d1a9b0aea058d28fd0862eeb2231de9151ffa36d291",
    "@pnpm/exe.win32-x64": "12c4d01cb418cc7e15a155f4d467545bd161b4b2bfe57022f155a5537dfe7c33",
}


def pnpm_tarball(name: str) -> str:
    """The registry's file name for one pinned pnpm package tarball."""
    return f"{name.rsplit('/', 1)[-1]}-{PNPM_VERSION}.tgz"


def _pnpm_host_package() -> str:
    machine = platform.machine().lower()
    architecture = {"aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    return f"@pnpm/exe.{sys.platform}-{architecture}"


class DocumentRuntimeError(RuntimeError):
    """The prepared runtime could not be installed or verified."""


def _document_uv() -> str | None:
    """Prefer Clio's bundled uv, then PATH or the installed distribution's scripts."""
    bundled = bundled_root()
    if bundled is not None:
        candidate = bundled / "bin" / ("uv.exe" if os.name == "nt" else "uv")
        if candidate.is_file():
            return str(candidate)
    found = _uv_executable(sys.executable)
    if found:
        return found
    packaged = Path(sys.executable).parent / ("uv.exe" if os.name == "nt" else "uv")
    return str(packaged) if packaged.is_file() else None


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 180,
    accepted_exit_codes: tuple[int, ...] = (0,),
) -> str:
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **flags,
        )
    except OSError as exc:
        raise DocumentRuntimeError(f"Could not launch {Path(command[0]).name}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        import psutil

        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        process.communicate()
        raise DocumentRuntimeError(f"{Path(command[0]).name} exceeded {timeout:g} seconds") from exc
    # A nonzero exit with no output is a launch failure (e.g. a sandbox wrapper that could not
    # start the child), not a reported result: surface its stderr instead of an empty parse.
    if process.returncode not in accepted_exit_codes or (process.returncode and not stdout.strip()):
        raise DocumentRuntimeError(f"{Path(command[0]).name} failed: {(stderr or stdout)[-4000:]}")
    return stdout


def _fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(f"{sys.implementation.cache_tag}:{sysconfig.get_platform()}".encode())
    for name in ("pyproject.toml", "uv.lock", "package.json", "pnpm-lock.yaml"):
        digest.update((STACK_ROOT / name).read_bytes())
    return digest.hexdigest()[:20]


def _probe(python: Path, *, discovery_only: bool = False) -> dict[str, Any]:
    result = json.loads(
        _run(
            [
                str(python),
                "-B",
                str(STACK_ROOT / "inventory.py"),
                *(["--execution-only"] if discovery_only else []),
            ],
            cwd=STACK_ROOT,
            env={
                **os.environ,
                "CLIO_DOCUMENT_OFFICE_ROOT": str(office_runtime_root()),
                "PYTHONDONTWRITEBYTECODE": "1",
                # The inventory imports matplotlib, which otherwise creates the user's
                # ~/.config/matplotlib; it belongs to this environment, inside CLIO's cache.
                "MPLCONFIGDIR": str(python.parents[1] / ".matplotlib"),
            },
        )
    )
    if not isinstance(result, dict):
        raise DocumentRuntimeError("Document inventory was not a JSON object")
    lock = tomllib.loads((STACK_ROOT / "uv.lock").read_text(encoding="utf-8"))
    expected = {package["name"]: package.get("version") for package in lock["package"]}
    for name, package in result.get("packages", {}).items():
        if package["version"] != expected.get(name):
            raise DocumentRuntimeError(f"Prepared package {name} differs from its locked version")
    return result


def inspect_python_imports(python: Path, modules: list[str]) -> dict[str, dict[str, str]]:
    """Check requested modules in the managed interpreter without installing anything."""
    names = list(dict.fromkeys(modules))
    if len(names) > 32 or any(
        len(name) > 200 or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name) is None
        for name in names
    ):
        raise ValueError("required_imports must contain at most 32 Python module names")
    script = str(STACK_ROOT / "import_check.py")
    output = _run([str(python), "-B", script, json.dumps(names)], cwd=python.parent)
    checks = json.loads(output)
    if not isinstance(checks, dict) or set(checks) != set(names):
        raise DocumentRuntimeError("Import checks did not return the requested modules")
    return checks


def inspect_execution_runtime(workspace: Path) -> dict[str, Any]:
    """Inspect existing tools without installing dependencies or creating runtime caches."""
    workspace = workspace.resolve(strict=True)
    inventory = json.loads(
        _run(
            [sys.executable, "-B", str(STACK_ROOT / "inventory.py"), "--execution-host"],
            cwd=workspace,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    )
    if not isinstance(inventory, dict) or inventory.get("inventory_scope") != "execution_host":
        raise DocumentRuntimeError("Execution inventory did not describe the current host")
    uv = _document_uv()
    inventory.update(
        {
            "status": "ready",
            "distribution": "execution-host",
            "python_argv": [inventory["python"], "-B"],
            "uv": uv,
            "uv_argv": [uv] if uv else [],
            "workspace": str(workspace),
            "installs_dependencies": False,
        }
    )
    return inventory


PRELAID_STAMP = "clio-prelaid.json"


def _adopt_prelaid(name: str, target: Path) -> bool:
    """Move a tree the runtime build laid out for this exact stack into ``target``.

    Windows runtime packs carry the document environment and a hoisted ``node_modules``
    (``install/build_document_env.py``), so an installation writes them while it unpacks
    instead of installing tens of thousands of files again. Adoption is one directory
    rename on the same volume; anything else (no tree, another stack, another volume)
    returns ``False`` with nothing changed, and the caller installs as usual.
    """
    runtime = bundled_root()
    if runtime is None or os.path.lexists(target):
        return False
    tree = runtime / "documents" / name
    try:
        stamp = json.loads((tree / PRELAID_STAMP).read_text(encoding="utf-8"))
        if not isinstance(stamp, dict) or stamp.get("fingerprint") != _fingerprint():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tree.rename(target)
    except (OSError, ValueError):
        return False
    (target / PRELAID_STAMP).unlink(missing_ok=True)
    return True


def _installer_wheel_root() -> Path | None:
    """The installer's downloads folder when it holds a runtime archive's document wheels.

    ``install.sh`` installs a Linux release from its prebuilt runtime archive and hands the
    archive's ``document-wheels`` over here, so this environment is laid out from the same
    locked wheels the desktop uses instead of downloading them again.
    ``bundled_wheel_requirements`` checks every wheel against this stack's lock.
    """
    from clio_agent.runtime.document_stack.office import INSTALLER_DOWNLOADS

    folder = os.environ.get(INSTALLER_DOWNLOADS, "")
    root = Path(folder) if folder else None
    return root if root and (root / "document-wheels" / "manifest.json").is_file() else None


def _filesystem(path: Path) -> int | None:
    """The device holding ``path``'s nearest existing ancestor, if it is writable."""
    for candidate in (path, *path.parents):
        if os.path.exists(candidate):
            return os.stat(candidate).st_dev if os.access(candidate, os.W_OK) else None
    return None


def _document_uv_cache(uv: str, home: Path, *, bundled: bool) -> Path:
    """The uv cache for the document environment beside ``home``.

    A release lays the environment out from the runtime's bundled wheels, so its cache is
    CLIO's own: the ~500 MB it fills go with ``uninstall --purge-caches`` instead of
    staying in ``~/.cache/uv`` after CLIO is gone. A source install reuses uv's own cache
    when it is on the same filesystem: the agent's ``uv sync`` already downloaded numpy,
    matplotlib, lxml and the rest there, so they are linked rather than downloaded again.
    A cache on another drive would copy every file instead, so that keeps the private one.
    """
    private = home / "uv-document-cache"
    if bundled:
        return private
    shared = _uv_cache_dir(uv)
    if shared is None:
        return private
    device = _filesystem(shared)
    return shared if device is not None and device == _filesystem(home) else private


def _python_runtime(
    cache: Path, uv: str, *, discovery_only: bool = False
) -> tuple[Path, dict[str, Any]]:
    project = cache / "project"
    project.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(STACK_ROOT / name, project / name)
    environment = cache / "python"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not os.path.lexists(environment) and _adopt_prelaid("python", environment):
        _set_environment_home(environment)
    repair = python.is_file() and _built_for_this_interpreter(environment)
    if not repair and os.path.lexists(environment):
        # A dangling or foreign base cannot be repaired in place: ``uv venv`` refuses an
        # existing directory, and reusing it would keep another installation's Python.
        try:
            shutil.rmtree(environment)
        except OSError as error:
            raise DocumentRuntimeError(
                f"Could not replace the stale document environment at {environment}: {error}"
            ) from error

    def probe() -> dict[str, Any]:
        return _probe(python, discovery_only=True) if discovery_only else _probe(python)

    if repair:
        try:
            return python, probe()
        except (DocumentRuntimeError, ValueError):
            # A damaged cache is repaired by the same locked sync as first use.
            pass
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "UV_CACHE_DIR": str(
            _document_uv_cache(
                uv,
                cache.parent,
                bundled=bundled_root() is not None or _installer_wheel_root() is not None,
            )
        ),
    }
    runtime = bundled_root()
    try:
        requirements = bundled_wheel_requirements(runtime, STACK_ROOT, project)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise DocumentRuntimeError(f"Bundled document packages are invalid: {error}") from error
    if runtime is None and (runtime := _installer_wheel_root()) is not None:
        try:
            requirements = bundled_wheel_requirements(runtime, STACK_ROOT, project)
        except (OSError, ValueError, KeyError, TypeError):
            # Wheels for another interpreter or lock: the locked sync below downloads them.
            runtime = requirements = None
    if requirements is not None:
        assert runtime is not None  # A verified wheel bundle can only come from a runtime.
        if not repair:
            _run(
                [uv, "venv", "--no-managed-python", "--python", sys.executable, str(environment)],
                cwd=project,
                env=env,
            )
        _run(
            [
                uv,
                "pip",
                "sync",
                "--python",
                str(python),
                str(requirements),
                "--no-index",
                "--find-links",
                str(runtime / "document-wheels"),
                "--require-hashes",
                "--only-binary=:all:",
                *(["--reinstall"] if repair else []),
            ],
            cwd=project,
            env=env,
        )
        return python, probe()
    _run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-install-project",
            "--no-managed-python",
            "--python",
            sys.executable,
            "--project",
            str(project),
            *(["--reinstall"] if repair else []),
        ],
        cwd=project,
        env=env,
    )
    return python, probe()


def _pnpm_command(package: Path, node: str, env: dict[str, str]) -> Path:
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if metadata.get("version") != PNPM_VERSION:
        raise DocumentRuntimeError("Prepared pnpm differs from its pinned version")
    pnpm = (package / metadata["bin"]["pnpm"]).resolve(strict=True)
    if not pnpm.is_relative_to(package.resolve()):
        raise DocumentRuntimeError("Installed pnpm command escapes its package directory")
    if _run([node, str(pnpm), "--version"], cwd=package, env=env).strip() != PNPM_VERSION:
        raise DocumentRuntimeError("Prepared pnpm did not execute its pinned version")
    return pnpm


def _installer_pnpm(package: Path) -> bool:
    """Unpack pnpm and its native binary from the installer's downloads, offline."""
    from clio_agent.runtime.document_stack.office import installer_download

    host = _pnpm_host_package()
    if host not in PNPM_PACKAGES:
        return False
    archives = {
        name: installer_download(pnpm_tarball(name), PNPM_PACKAGES[name]) for name in ("pnpm", host)
    }
    if any(archive is None for archive in archives.values()):
        return False
    modules = package.parent
    for name, archive in archives.items():
        assert archive is not None
        _unpack_package(archive, modules / name)
    return True


def _prepared_pnpm(
    tools: Path,
    node: str,
    env: dict[str, str],
    progress: Callable[[str], None] | None,
) -> Path:
    """The pinned pnpm, from the installer's downloads or else the registry."""
    package = tools / "node_modules" / "pnpm"
    try:
        return _pnpm_command(package, node, env)
    except (DocumentRuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        tools.mkdir(parents=True, exist_ok=True)
        try:
            if _installer_pnpm(package):
                return _pnpm_command(package, node, env)
        except (
            DocumentRuntimeError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            tarfile.TarError,
        ) as exc:
            # The registry install below is the same pinned version; only slower.
            if progress is not None:
                progress(f"The downloaded pnpm could not be used ({exc}); fetching it instead...")
    node_root = Path(node).parent
    if node_root.name == "bin":
        node_root = node_root.parent
    npm = node_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm.is_file():
        raise DocumentRuntimeError("Managed Node.js package has no npm entry point") from None
    if progress is not None:
        progress("Installing the pinned pnpm package manager...")
    _run(
        [
            node,
            str(npm),
            "install",
            "--prefix",
            str(tools),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            f"pnpm@{PNPM_VERSION}",
        ],
        cwd=tools,
        env=env,
    )
    try:
        return _pnpm_command(package, node, env)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DocumentRuntimeError(f"Installed pnpm has invalid metadata: {exc}") from exc


def _javascript_runtime(
    cache: Path,
    workspace: Path,
    inventory: dict[str, Any],
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path]:
    node = node_path(str(inventory["node"]))
    tools = Path(node_path(str(cache / "javascript-tools")))
    # Besides its store, pnpm keeps a metadata cache, its state and a global folder in the
    # user's XDG folders (~/.cache/pnpm, ~/.local/state/pnpm, ~/.local/share/pnpm). They are
    # this pnpm's, so they go in CLIO's cache, which uninstall --purge-caches removes.
    pnpm_home = Path(node_path(str(cache / "pnpm-home")))
    env = {
        **os.environ,
        "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": str(pnpm_home / "cache"),
        "XDG_STATE_HOME": str(pnpm_home / "state"),
        "XDG_DATA_HOME": str(pnpm_home / "data"),
    }
    work = Path(node_path(str(scratch_root(workspace) / "javascript")))
    store = node_path(str(cache / "pnpm-store"))
    # pnpm is needed only to install or repair the packages. It is prepared and
    # checked while prelaid packages are adopted and probed, rather than before.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="clio-node") as running:
        pnpm_ready = running.submit(_prepared_pnpm, tools, node, env, progress)
        work.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "pnpm-lock.yaml"):
            target = work / name
            expected = (STACK_ROOT / name).read_bytes()
            if target.exists() and target.read_bytes() != expected:
                raise DocumentRuntimeError(
                    f"Prepared workspace manifest was changed: {target}. Preserve task dependencies in a separate project."
                )
            target.write_bytes(expected)
        if not (work / "node_modules").is_dir() and _adopt_prelaid(
            "node_modules", work / "node_modules"
        ):
            # Keep later pnpm commands in this project on the layout it was built with.
            (work / ".npmrc").write_text("node-linker=hoisted\n", encoding="utf-8")
        if not (work / "node_modules").is_dir():
            if progress is not None:
                progress("Installing Word, PowerPoint and image packages for Node.js...")
            _run(
                [
                    node,
                    str(pnpm_ready.result()),
                    "install",
                    "--frozen-lockfile",
                    "--ignore-scripts",
                    "--store-dir",
                    store,
                ],
                cwd=work,
                env=env,
            )
        # An interrupted install or damaged package gets one locked repair attempt.
        probe = [
            node,
            "-e",
            "require('docx'); require('pptxgenjs'); require('sharp'); "
            "const fs = require('fs'); const expected = JSON.parse(fs.readFileSync('package.json')).dependencies; "
            "for (const [name, version] of Object.entries(expected)) { "
            "const actual = JSON.parse(fs.readFileSync('node_modules/' + name + '/package.json')).version; "
            "if (actual !== version) throw new Error(name + ' differs from its locked version'); }",
        ]
        module_probe = [
            node,
            "--input-type=module",
            "-e",
            "await import('docx'); await import('pptxgenjs'); await import('sharp');",
        ]
        # The CommonJS and ES module checks are independent node processes.
        modules_checked = running.submit(_run, module_probe, cwd=work, env=env)
        try:
            _run(probe, cwd=work, env=env)
        except DocumentRuntimeError:
            try:
                modules_checked.result()
            except DocumentRuntimeError:
                pass
            _run(
                [
                    node,
                    str(pnpm_ready.result()),
                    "install",
                    "--force",
                    "--frozen-lockfile",
                    "--ignore-scripts",
                    "--store-dir",
                    store,
                ],
                cwd=work,
                env=env,
            )
            _run(probe, cwd=work, env=env)
            modules_checked = running.submit(_run, module_probe, cwd=work, env=env)
        modules_checked.result()
        pnpm = pnpm_ready.result()
    return work, pnpm


def _javascript_or_error(
    cache: Path,
    workspace: Path,
    inventory: dict[str, Any],
    progress: Callable[[str], None] | None,
) -> tuple[Path, Path] | DocumentRuntimeError:
    """Prepare Node.js; a failure is returned so the verified Python stack stays usable."""
    try:
        return _javascript_runtime(cache, workspace, inventory, progress=progress)
    except DocumentRuntimeError as exc:
        return exc


def prepare_document_runtime(
    workspace: Path,
    *,
    cache_root: Path | None = None,
    progress: Callable[[str], None] | None = None,
    discovery_only: bool = False,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Prepare and probe an isolated document runtime without changing Clio's interpreter.

    First use downloads Node.js and the locked package sets using Clio's Python. A failed
    JavaScript setup leaves the independently verified Python stack available.
    Native converters are discovered and reported, never advertised as installed.
    ``discovery_only`` freshly inspects executables and package metadata, without
    importing every package or probing document converters/fonts. Requested imports
    are independently checked by the execution tool; no prior answer is reused.
    ``timings``, when given, receives the seconds of the Python environment, the
    Node.js packages and the Python import probe.
    """

    def lap(key: str, started: float) -> None:
        if timings is not None:
            timings[key] = round(time.monotonic() - started, 1)

    workspace = workspace.resolve(strict=True)
    from clio_agent.tools.file_policy import FileAccessPolicy

    policy = FileAccessPolicy.from_env()
    scratch = scratch_root(workspace, create=False)
    for name in ("output", "javascript"):
        policy.validate_write(str(scratch / name / "manifest.json"), create_parent=True)
    uv = _document_uv()
    if not uv:
        raise DocumentRuntimeError(
            "uv is unavailable on the execution host; install uv or use a Clio runtime bundle that includes it"
        )
    cache = (cache_root or paths.user_cache_dir() / "document-runtime") / _fingerprint()
    cache.mkdir(parents=True, exist_ok=True)
    with FileLock(str(cache / "prepare.lock"), timeout=240):
        if progress is not None:
            progress("Preparing and checking Python document packages...")
        started = time.monotonic()
        python, inventory = _python_runtime(cache, uv, discovery_only=True)
        lap("python_environment", started)
        javascript: tuple[Path, Path] | DocumentRuntimeError
        if discovery_only:
            if progress is not None:
                progress("Python document packages are ready. Preparing Node.js packages...")
            started = time.monotonic()
            javascript = _javascript_or_error(cache, workspace, inventory, progress)
            lap("node_packages", started)
        else:
            # Node needs only the installed environment, so it is prepared while the
            # full import probe runs. That probe is most of the Python step: the first
            # load of each newly installed native library is checked by the OS.
            if progress is not None:
                progress("Python document packages are installed. Preparing Node.js packages...")
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clio-probe") as checking:
                started = time.monotonic()

                def probe() -> dict[str, Any]:
                    try:
                        return _probe(python)
                    finally:
                        lap("python_probe", started)

                probed = checking.submit(probe)
                javascript = _javascript_or_error(cache, workspace, inventory, progress)
                lap("node_packages", started)
                try:
                    inventory = probed.result()
                except (DocumentRuntimeError, ValueError):
                    inventory = {}
            if not inventory:
                # A damaged environment gets the same locked repair as before, now that
                # nothing runs from it.
                python, inventory = _python_runtime(cache, uv)
            if progress is not None:
                progress("Python document packages are ready.")
        output = scratch / "output"
        output.mkdir(parents=True, exist_ok=True)
        inventory.update(
            {
                "status": "ready",
                "runtime_id": cache.name,
                "uv": uv,
                "uv_argv": [uv],
                "office_root": str(office_runtime_root()),
                "office_profiles": str(office_profile_templates()),
                "distribution": "managed-cache",
                "helper": str(STACK_ROOT / "cli.py"),
                "output_directory": str(output),
                "python_argv": [
                    uv,
                    "run",
                    "--no-project",
                    "--python",
                    str(python),
                    "python",
                ],
                "helper_argv": [str(python), str(STACK_ROOT / "cli.py")],
                "capabilities": {
                    "pdf_extract_render": "ready",
                    "office_create_inspect": "ready",
                    "office_render_recalculate": inventory["native_tools"]["soffice"]["status"],
                    "ocr": inventory["native_tools"]["tesseract"]["status"],
                },
            }
        )
        if isinstance(javascript, DocumentRuntimeError):
            inventory["javascript"] = {"status": "failed", "error": str(javascript)}
            inventory["status"] = "partial"
        else:
            work, pnpm = javascript
            inventory["node"] = node_path(str(inventory["node"]))
            inventory["javascript"] = {
                "status": "ready",
                "workspace": str(work),
                "node_argv": [inventory["node"]],
                "pnpm_argv": [inventory["node"], str(pnpm)],
                "script_argv": [
                    inventory["node"],
                    str(pnpm),
                    "--dir",
                    str(work),
                    "exec",
                    inventory["node"],
                ],
                "packages": json.loads((STACK_ROOT / "package.json").read_text())["dependencies"],
            }
        started = time.monotonic()
        publish_environment(workspace, inventory, cache)
        lap("publish", started)
        if progress is not None and inventory["javascript"]["status"] == "ready":
            progress("Node.js document packages are ready.")
    return inventory


def office_runtime_root() -> Path:
    """Select the relocated bundled renderer or this user's private cache."""
    from clio_agent.runtime.document_stack.office import VERSION, executable

    bundled = bundled_root()
    if bundled is not None and executable(bundled / "documents" / "office"):
        return bundled / "documents" / "office"
    return paths.user_cache_dir() / "document-office" / VERSION


def office_profile_templates() -> Path:
    """Hold each Office build's seeded first-start profile for this user."""
    return paths.user_cache_dir() / "document-office-profiles"


def office_profile_starter(office_root: Path) -> Path | None:
    """Return the first-start profile laid out beside the bundled renderer, if any."""
    from clio_agent.runtime.document_stack.office import PROFILE_STARTER

    bundled = bundled_root()
    if bundled is None or office_root != bundled / "documents" / "office":
        return None
    starter = office_root.parent / PROFILE_STARTER
    return starter if starter.is_file() else None


def prepare_office_runtime() -> str:
    """Use an installed renderer or provision a private verified renderer automatically."""
    from clio_agent.runtime.document_stack.office import ensure_office, seed_profile
    from clio_agent.runtime.document_stack.process import find_native

    managed_root = office_runtime_root()
    native = find_native("soffice")
    if native and not Path(native).resolve().is_relative_to(managed_root.resolve()):
        seed_profile(Path(native), office_profile_templates())
        return native
    # A discovered managed executable still needs its host-side access repair.
    return ensure_office(
        managed_root,
        profile_templates=office_profile_templates(),
        profile_starter=office_profile_starter(managed_root),
    )


def run_document_helper(runtime: dict[str, Any], args: list[str], *, cwd: Path) -> dict[str, Any]:
    """Run the portable helper through the verified interpreter and parse its result."""
    from clio_agent.runtime import sandbox

    command = [*runtime["helper_argv"], *args]
    environment = {**os.environ, **runtime.get("shell_environment", {})}
    office = bool(args) and (
        args[0] == "recalculate"
        or (args[0] == "render" and len(args) > 1 and Path(args[1]).suffix.lower() != ".pdf")
    )
    if office:
        # The trusted host owns renderer provisioning. A confined document worker
        # must consume the verified executable, never run Windows Installer.
        environment["CLIO_DOCUMENT_SOFFICE"] = prepare_office_runtime()
    environment["CLIO_DOCUMENT_CONFINED"] = "1"
    confined = sandbox.wrap_confined(
        command[0],
        command[1:],
        write_roots=sandbox.effective_write_roots(sandbox.PROFILE_SHELL, workspace_root=str(cwd)),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_SHELL,
        pdeathsig=False,
        office=office,
    )
    try:
        result = json.loads(
            _run(
                [confined.command, *confined.args],
                cwd=cwd,
                env={
                    **environment,
                    **confined.env_overlay,
                    **{
                        key: value
                        for key, value in runtime.get("shell_environment", {}).items()
                        if key in {"TEMP", "TMP", "TMPDIR"}
                    },
                },
                accepted_exit_codes=(0, 1),
            )
        )
    except json.JSONDecodeError as exc:
        raise DocumentRuntimeError("Document helper exited without a result") from exc
    finally:
        confined.release()
    if not isinstance(result, dict):
        raise DocumentRuntimeError("Document helper returned no structured result")
    return result
