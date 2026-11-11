"""Lay out the document environment and Node packages inside a Windows runtime.

An installation then writes these files while it unpacks the runtime pack, and the
first prepare adopts them with one directory rename (``document_runtime._adopt_prelaid``)
instead of creating about 10,000 files again with uv and pnpm. Each tree carries the
stack fingerprint it was built for; a runtime whose stack differs ignores it.

Run with the runtime's own interpreter, after its document wheels are built::

    <runtime>\\python\\python.exe -I -B install/build_document_env.py --runtime <runtime> --uv uv.exe
"""

from __future__ import annotations

import argparse
import compileall
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from clio_agent.runtime.document_runtime import (
    PNPM_VERSION,
    PRELAID_STAMP,
    STACK_ROOT,
    _fingerprint,
)
from clio_agent.runtime.document_wheels import bundled_wheel_requirements


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("[document-env]", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _stamp(tree: Path) -> None:
    (tree / PRELAID_STAMP).write_text(
        json.dumps({"fingerprint": _fingerprint()}) + "\n", encoding="utf-8"
    )


def _python(runtime: Path, uv: str, work: Path) -> Path:
    environment = runtime / "documents" / "python"
    project = work / "project"
    project.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(STACK_ROOT / name, project / name)
    requirements = bundled_wheel_requirements(runtime, STACK_ROOT, project)
    if requirements is None:
        raise SystemExit(f"{runtime} has no verified document wheels")
    # Copies, not links into a cache: the tree is packed and moved as it is.
    env = {**os.environ, "UV_CACHE_DIR": str(work / "uv-cache"), "UV_LINK_MODE": "copy"}
    _run(
        [uv, "venv", "--relocatable", "--no-managed-python", "--python", sys.executable,
         str(environment)],
        cwd=project,
        env=env,
    )
    python = environment / "Scripts" / "python.exe"
    _run(
        [uv, "pip", "sync", "--python", str(python), str(requirements), "--no-index",
         "--find-links", str(runtime / "document-wheels"), "--require-hashes",
         "--only-binary=:all:"],
        cwd=project,
        env=env,
    )
    # Hash-based bytecode: the pack does not carry modification times, so uv's
    # timestamp .pyc files would all be stale once unpacked and every import would
    # compile from source (document probe 5.2 s -> 1.4 s).
    compileall.compile_dir(
        environment / "Lib" / "site-packages",
        quiet=1,
        workers=0,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    _stamp(environment)
    return python


def _node_modules(runtime: Path, python: Path, work: Path) -> None:
    inventory = json.loads(subprocess.run(
        [str(python), "-B", str(STACK_ROOT / "inventory.py"), "--execution-only"],
        check=True, capture_output=True, text=True, cwd=STACK_ROOT,
    ).stdout)
    node = str(inventory["node"])
    root = Path(node).parent
    if root.name == "bin":
        root = root.parent
    npm = root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    tools = work / "tools"
    tools.mkdir()
    _run([node, str(npm), "install", "--prefix", str(tools), "--ignore-scripts", "--no-audit",
          "--no-fund", f"pnpm@{PNPM_VERSION}"], cwd=tools)
    package = tools / "node_modules" / "pnpm"
    pnpm = package / json.loads((package / "package.json").read_text())["bin"]["pnpm"]
    project = work / "javascript"
    project.mkdir()
    for name in ("package.json", "pnpm-lock.yaml"):
        shutil.copyfile(STACK_ROOT / name, project / name)
    # Hoisted real directories (no junctions) and copied files (no hard links): the
    # runtime pack carries only plain files and directories.
    _run([node, str(pnpm), "install", "--frozen-lockfile", "--ignore-scripts",
          "--config.node-linker=hoisted", "--config.package-import-method=copy",
          "--store-dir", str(work / "pnpm-store")], cwd=project)
    target = runtime / "documents" / "node_modules"
    shutil.move(str(project / "node_modules"), str(target))
    _stamp(target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--uv", required=True)
    parser.add_argument("--drop-wheels", action="store_true",
                        help="Remove document-wheels once the environment is laid out.")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    for name in ("python", "node_modules"):
        shutil.rmtree(runtime / "documents" / name, ignore_errors=True)
    (runtime / "documents").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="document-env-") as temporary:
        work = Path(temporary)
        python = _python(runtime, args.uv, work)
        _node_modules(runtime, python, work)
    if args.drop_wheels:
        shutil.rmtree(runtime / "document-wheels")
    for name in ("python", "node_modules"):
        tree = runtime / "documents" / name
        files = sum(1 for path in tree.rglob("*") if path.is_file())
        print(f"[document-env] {tree}: {files} files", flush=True)


if __name__ == "__main__":
    main()
