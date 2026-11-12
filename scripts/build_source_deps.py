"""Build a source install's locked dependencies once and pack them as one release asset.

Windows x64 only. ``install.ps1 --source`` otherwise spends minutes on ``uv sync``:
fetching and laying ~200 wheels, then compiling their bytecode, file by file under
Defender. Here the same environment is built once: a relocatable virtualenv
(``uv venv --relocatable``, so the console-script launchers name ``python.exe`` beside
them, not this machine's path) holding every locked third-party package of the source
install's extras, with checked-hash bytecode (portable across relocation, and Python
recompiles any module whose source differs). The project itself is not installed: the
source install adds it as the editable install ``uv sync`` makes.

    uv run --no-project --python 3.14 --with zstandard \
        python scripts/build_source_deps.py --output DIR [--release-url URL]

Writes ``DIR/<asset>``, named ``clio-deps-<uv.lock SHA-256, 16 hex>-py<minor>-windows-x64.tar.zst``,
and ``DIR/source-deps.pack.json`` (``asset``, ``sha256``, ``archive_bytes``,
``lock_sha256``, ``python``, and ``url`` when ``--release-url`` names the release's
download folder), which ``scripts/release_install_plan.py --deps-pack`` reads.
``install.ps1`` takes the asset only when its name carries the digest of the clone's
own ``uv.lock`` and its Python minor version; then ``uv sync --locked`` over it installs just the project, and any
difference from the lock is still installed by ``uv sync``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import zstandard

ROOT = Path(__file__).resolve().parents[1]
#: The source install's extras (install.ps1 ``$syncArgs``).
EXTRAS = ("argonne", "flowcept")
#: The top folder of the archive's members; install.ps1 strips it.
TOP = "clio-deps"


def lock_digest(lock: Path) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def asset_name(lock_sha256: str, python: str) -> str:
    """The asset's name: the uv.lock digest and the Python minor version it was built for."""
    minor = "".join(python.split(".")[:2])
    return f"{TOP}-{lock_sha256[:16]}-py{minor}-windows-x64.tar.zst"


def _run(command: list[str], env: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def build(venv: Path, uv: str, python: str) -> None:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)}
    env.pop("VIRTUAL_ENV", None)
    _run(
        [uv, "venv", "--quiet", "--relocatable", "--managed-python", "--python", python, str(venv)],
        env,
    )
    sync = [uv, "sync", "--locked", "--no-install-project", "--no-progress"]
    for extra in EXTRAS:
        sync += ["--extra", extra]
    _run(sync, env)
    interpreter = venv / "Scripts" / "python.exe"
    _run(
        [
            str(interpreter),
            "-m",
            "compileall",
            "-q",
            "-j",
            "0",
            "--invalidation-mode",
            "checked-hash",
            str(venv / "Lib" / "site-packages"),
        ],
        env,
    )


#: Imported from the built environment before it is packed.
SMOKE_IMPORTS = ("pydantic", "fastapi", "httpx", "litellm", "docx", "openpyxl")


def verify(venv: Path) -> None:
    """Refuse to pack an environment missing any file a package's RECORD lists."""
    site = venv / "Lib" / "site-packages"
    broken: list[str] = []
    for info in sorted(site.glob("*.dist-info")):
        record = info / "RECORD"
        if not record.is_file():
            broken.append(f"{info.name}: no RECORD")
            continue
        for line in record.read_text(encoding="utf-8").splitlines():
            entry = line.split(",", 1)[0]
            if entry and not entry.startswith("..") and not (site / entry).exists():
                broken.append(f"{info.name}: {entry}")
                break
    if broken:
        raise SystemExit("incomplete environment:\n  " + "\n  ".join(broken))
    interpreter = venv / "Scripts" / "python.exe"
    subprocess.run(
        [str(interpreter), "-c", "; ".join(f"import {name}" for name in SMOKE_IMPORTS)],
        check=True,
    )


def _members(image: Path) -> list[Path]:
    found: list[Path] = []
    for directory, names, files in os.walk(image):
        names.sort()
        path = Path(directory)
        found.extend(path / name for name in names)
        found.extend(path / name for name in sorted(files))
    return found


def _owned_by_root(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def pack(venv: Path, target: Path, level: int) -> None:
    # 1.8 GB of input: level 19 took over ten minutes and 1.5 GB of memory; the
    # decompression that an install waits on is as fast at this level.
    threads = min(os.cpu_count() or 1, 8)
    compressor = zstandard.ZstdCompressor(level=level, threads=threads, write_content_size=False)
    with (
        target.open("wb") as raw,
        compressor.stream_writer(raw) as stream,
        tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in _members(venv):
            # pyvenv.cfg names this machine's interpreter; install.ps1's ``uv venv
            # --allow-existing`` writes the installing machine's.
            if path.parent == venv and path.name == "pyvenv.cfg":
                continue
            relative = f"{TOP}/{path.relative_to(venv).as_posix()}"
            archive.add(path, relative, recursive=False, filter=_owned_by_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-url", help="the release's download folder URL")
    parser.add_argument("--level", type=int, default=12, help="zstd level")
    args = parser.parse_args(argv)
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("source-install dependencies are prepared for Windows x64 only")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is not on PATH")
    python = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    lock_sha256 = lock_digest(ROOT / "uv.lock")
    name = asset_name(lock_sha256, python)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / name
    with tempfile.TemporaryDirectory(prefix="source-deps-", dir=args.output) as temporary:
        venv = Path(temporary) / ".venv"
        build(venv, uv, python)
        version = subprocess.run(
            [
                str(venv / "Scripts" / "python.exe"),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        verify(venv)
        pack(venv, target, args.level)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "asset": name,
        "sha256": digest,
        "archive_bytes": target.stat().st_size,
        "lock_sha256": lock_sha256,
        "python": version,
    }
    if args.release_url:
        manifest["url"] = f"{args.release_url.rstrip('/')}/{name}"
    (args.output / "source-deps.pack.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
