"""Prepare Clio's isolated, locked document stack on the execution host."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths
from clio_agent.providers.dependencies import _uv_executable
from clio_agent.runtime.document_stack.process import scratch_root
from clio_agent.runtime.execution_environment import (
    bundled_root,
    publish_environment,
)

STACK_ROOT = Path(__file__).resolve().parent / "document_stack"
PNPM_VERSION = "12.9.1"


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
    if process.returncode not in accepted_exit_codes:
        raise DocumentRuntimeError(f"{Path(command[0]).name} failed: {(stderr or stdout)[-4000:]}")
    return stdout


def _fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(f"{sys.implementation.cache_tag}:{sysconfig.get_platform()}".encode())
    for name in ("pyproject.toml", "uv.lock", "package.json", "pnpm-lock.yaml"):
        digest.update((STACK_ROOT / name).read_bytes())
    return digest.hexdigest()[:20]


def _probe(python: Path) -> dict[str, Any]:
    result = json.loads(
        _run(
            [str(python), str(STACK_ROOT / "inventory.py")],
            cwd=STACK_ROOT,
            env={**os.environ, "CLIO_DOCUMENT_OFFICE_ROOT": str(office_runtime_root())},
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


def _python_runtime(cache: Path, uv: str) -> tuple[Path, dict[str, Any]]:
    project = cache / "project"
    project.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(STACK_ROOT / name, project / name)
    environment = cache / "python"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    repair = python.is_file()
    if repair:
        try:
            return python, _probe(python)
        except (DocumentRuntimeError, ValueError):
            # A damaged cache is repaired by the same locked sync as first use.
            pass
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)}
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
    return python, _probe(python)


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


def _javascript_runtime(
    cache: Path, workspace: Path, python: Path, inventory: dict[str, Any]
) -> tuple[Path, Path]:
    node = str(inventory["node"])
    tools = cache / "javascript-tools"
    package = tools / "node_modules" / "pnpm"
    env = {**os.environ, "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")}
    try:
        pnpm = _pnpm_command(package, node, env)
    except (DocumentRuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError):
        tools.mkdir(parents=True, exist_ok=True)
        _run(
            [
                str(python),
                "-c",
                "import sys; from nodejs_wheel import npm; sys.exit(npm(sys.argv[1:]))",
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
            pnpm = _pnpm_command(package, node, env)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DocumentRuntimeError(f"Installed pnpm has invalid metadata: {exc}") from exc
    work = scratch_root(workspace) / "javascript"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "pnpm-lock.yaml"):
        target = work / name
        expected = (STACK_ROOT / name).read_bytes()
        if target.exists() and target.read_bytes() != expected:
            raise DocumentRuntimeError(
                f"Prepared workspace manifest was changed: {target}. Preserve task dependencies in a separate project."
            )
        target.write_bytes(expected)
    if not (work / "node_modules").is_dir():
        _run(
            [
                node,
                str(pnpm),
                "install",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--store-dir",
                str(cache / "pnpm-store"),
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
    try:
        _run(probe, cwd=work, env=env)
    except DocumentRuntimeError:
        _run(
            [
                node,
                str(pnpm),
                "install",
                "--force",
                "--frozen-lockfile",
                "--ignore-scripts",
                "--store-dir",
                str(cache / "pnpm-store"),
            ],
            cwd=work,
            env=env,
        )
        _run(probe, cwd=work, env=env)
    _run(
        [
            node,
            "--input-type=module",
            "-e",
            "await import('docx'); await import('pptxgenjs'); await import('sharp');",
        ],
        cwd=work,
        env=env,
    )
    return work, pnpm


def prepare_document_runtime(workspace: Path, *, cache_root: Path | None = None) -> dict[str, Any]:
    """Prepare and probe an isolated document runtime without changing Clio's interpreter.

    First use downloads Node.js and the locked package sets using Clio's Python. A failed
    JavaScript setup leaves the independently verified Python stack available.
    Native converters are discovered and reported, never advertised as installed.
    """
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
        python, inventory = _python_runtime(cache, uv)
        output = scratch / "output"
        output.mkdir(parents=True, exist_ok=True)
        inventory.update(
            {
                "status": "ready",
                "runtime_id": cache.name,
                "uv": uv,
                "uv_argv": [uv],
                "office_root": str(office_runtime_root()),
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
                "skills": {
                    name: str(
                        Path(__file__).resolve().parents[1] / "gact" / "builtin_skills" / name
                    )
                    for name in (
                        "work-with-pdfs",
                        "work-with-word",
                        "work-with-presentations",
                        "work-with-spreadsheets",
                    )
                },
                "capabilities": {
                    "pdf_extract_render": "ready",
                    "office_create_inspect": "ready",
                    "office_render_recalculate": inventory["native_tools"]["soffice"]["status"],
                    "ocr": inventory["native_tools"]["tesseract"]["status"],
                },
            }
        )
        try:
            work, pnpm = _javascript_runtime(cache, workspace, python, inventory)
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
        except DocumentRuntimeError as exc:
            inventory["javascript"] = {"status": "failed", "error": str(exc)}
            inventory["status"] = "partial"
        publish_environment(workspace, inventory, cache)
    return inventory


def office_runtime_root() -> Path:
    """Select the relocated bundled renderer or this user's private cache."""
    from clio_agent.runtime.document_stack.office import VERSION, executable

    bundled = bundled_root()
    if bundled is not None and executable(bundled / "documents" / "office"):
        return bundled / "documents" / "office"
    return paths.user_cache_dir() / "document-office" / VERSION


def prepare_office_runtime() -> str:
    """Use an installed renderer or provision a private verified renderer automatically."""
    from clio_agent.runtime.document_stack.office import ensure_office
    from clio_agent.runtime.document_stack.process import find_native

    return find_native("soffice") or ensure_office(office_runtime_root())


def run_document_helper(runtime: dict[str, Any], args: list[str], *, cwd: Path) -> dict[str, Any]:
    """Run the portable helper through the verified interpreter and parse its result."""
    from clio_agent.runtime import sandbox

    command = [*runtime["helper_argv"], *args]
    confined = sandbox.wrap_confined(
        command[0],
        command[1:],
        write_roots=sandbox.effective_write_roots(sandbox.PROFILE_SHELL, workspace_root=str(cwd)),
        net_policy=sandbox.NET_ALLOW_RECORD,
        profile=sandbox.PROFILE_SHELL,
        pdeathsig=False,
    )
    result = json.loads(
        _run(
            [confined.command, *confined.args],
            cwd=cwd,
            env={
                **os.environ,
                **runtime.get("shell_environment", {}),
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
    if not isinstance(result, dict):
        raise DocumentRuntimeError("Document helper returned no structured result")
    return result
