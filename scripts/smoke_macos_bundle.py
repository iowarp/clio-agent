#!/usr/bin/env python3
"""Verify the actual macOS app and launch it with its real packaged sidecar.

Run against a relocated copy from a mounted DMG or extracted updater archive.
This tests bundle integrity and runtime compatibility. ``--quarantine`` marks the
copy as downloaded and records Gatekeeper's verdict; the quarantined boot runs
when Gatekeeper accepts the app, and ``--require-gatekeeper`` makes a rejection
fatal (use it once releases are notarized).

``--macho-list`` writes every Mach-O inside ``Resources/gact-runtime`` (found by
magic number, not by name) plus binaries hidden inside archives, which the
notary service also inspects. The signing step must sign each listed file
inside-out with the hardened runtime; see ``install/macos-signing.md``.

``--exercise`` runs the installer's ``--prepare-runtime`` step first, then has the
live backend serve local web search and an Office render. After each stage the
seal must verify, nothing in the bundle may change, and no ``__pycache__/*.pyc``
may be newer than ``Info.plist`` (#1979).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import signal
import socket
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def verify(app: Path) -> None:
    """Require an intact application seal and an executable for this host."""
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app)], check=True
    )
    with (app / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    if metadata["CFBundleIdentifier"] != "ai.iowarp.clio.desktop":
        raise ValueError("Unexpected application identity")
    subprocess.run(["file", str(app / "Contents/MacOS/clio-desktop")], check=True)


_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
}
_FAT_MAGICS = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
_ARCHIVE_SUFFIXES = (".whl", ".zip", ".jar", ".egg")


def is_macho(header: bytes) -> bool:
    """Recognise thin and universal Mach-O headers; a Java class shares 0xCAFEBABE."""
    if header[:4] in _MACHO_MAGICS:
        return True
    # A universal header's second word is its small arch count; a class file's
    # is its version (>= 45), which is how file(1) tells them apart.
    return header[:4] in _FAT_MAGICS and 0 < int.from_bytes(header[4:8], "big") < 45


def macho_inventory(runtime: Path) -> dict[str, list[str]]:
    """Every Mach-O the signing step must sign, and archive members it cannot."""
    files: list[str] = []
    archived: list[str] = []
    for path in sorted(runtime.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(runtime).as_posix()
        with path.open("rb") as stream:
            if is_macho(stream.read(8)):
                files.append(relative)
                continue
        if path.suffix in _ARCHIVE_SUFFIXES and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if member.is_dir() or member.file_size < 8:
                        continue
                    with archive.open(member) as stream:
                        if is_macho(stream.read(8)):
                            archived.append(f"{relative}!{member.filename}")
    return {"files": files, "archived": archived}


def snapshot(app: Path) -> dict[str, tuple[int, int]]:
    """Size and mtime of every file, to prove a run wrote nothing into the bundle."""
    return {
        path.relative_to(app).as_posix(): (stat.st_size, stat.st_mtime_ns)
        for path in app.rglob("*")
        if (stat := path.lstat()) and not path.is_dir()
    }


def require_unchanged(app: Path, before: dict[str, tuple[int, int]], stage: str) -> None:
    after = snapshot(app)
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if changed:
        raise RuntimeError(f"{stage} wrote into the bundle ({len(changed)} paths): {changed[:20]}")


def written_bytecode(app: Path) -> list[str]:
    """Bytecode compiled after the app was built, which breaks its signature seal (#1979)."""
    built = (app / "Contents/Info.plist").stat().st_mtime_ns
    return sorted(
        path.relative_to(app).as_posix()
        for path in app.rglob("__pycache__/*.pyc")
        if path.lstat().st_mtime_ns > built
    )


def require_sealed(app: Path, before: dict[str, tuple[int, int]], stage: str) -> None:
    """Fail if ``stage`` compiled bytecode or changed anything inside the signed bundle."""
    written = written_bytecode(app)
    if written:
        raise RuntimeError(
            f"{stage} compiled bytecode into the bundle ({len(written)} files): {written[:20]}"
        )
    require_unchanged(app, before, stage)
    verify(app)
    print(f"Bundle seal intact after {stage}", flush=True)


def prepare_runtime(app: Path, env: dict[str, str]) -> None:
    """Run the installer's ``--prepare-runtime`` step: the document stack and Office."""
    result = subprocess.run(
        [str(app / "Contents/MacOS/clio-desktop"), "--prepare-runtime"],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    print(result.stdout + result.stderr, flush=True)
    if result.returncode:
        raise RuntimeError(f"--prepare-runtime exited {result.returncode}")


def get_json(url: str, token: str | None = None) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        return json.load(response)


def find_record(value: object, record_id: str) -> dict[str, object] | None:
    if isinstance(value, dict):
        if value.get("id") == record_id:
            return value
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            if (found := find_record(item, record_id)) is not None:
                return found
    return None


def searxng_operations(inventory: object) -> list[dict[str, object]]:
    operations = inventory.get("operations") if isinstance(inventory, dict) else None
    return [
        row
        for row in operations or []
        if isinstance(row, dict) and row.get("service_id") == "searxng"
    ]


def dump_operation_logs(port: int, token: str, operations: list[dict[str, object]]) -> None:
    """Print each SearXNG operation's newest log lines, for a run that never served."""
    for row in operations:
        print(f"SearXNG operation {row.get('action')} {row.get('id')}: {row}", flush=True)
        try:
            log = get_json(
                f"http://127.0.0.1:{port}/v1/infrastructure/operations/{row.get('id')}/log"
                "?limit=5000",
                token,
            )
        except (HTTPError, URLError, TimeoutError) as error:
            print(f"  log unavailable: {error}", flush=True)
            continue
        lines = log.get("lines") if isinstance(log, dict) else None
        for line in (lines or [])[-80:]:
            if isinstance(line, dict):
                print(f"  {line.get('at')} {line.get('line')}", flush=True)


def use_web_search(port: int, token: str) -> None:
    """Wait for the boot-installed local SearXNG worker, then search through it."""
    deadline = time.monotonic() + 300
    record: dict[str, object] | None = None
    operations: list[dict[str, object]] = []
    reported: set[tuple[object, ...]] = set()
    while time.monotonic() < deadline:
        try:
            inventory = get_json(f"http://127.0.0.1:{port}/v1/infrastructure/inventory", token)
        except HTTPError as error:
            # The server answers 503 "starting" until its boot work settles.
            if error.code != 503:
                raise
            time.sleep(3)
            continue
        record = find_record(inventory, "local:searxng")
        operations = searxng_operations(inventory)
        for row in operations:
            key = (row.get("id"), row.get("state"), row.get("progress"))
            if key not in reported:
                reported.add(key)
                print(
                    f"SearXNG {row.get('action')}: {row.get('state')} {row.get('progress')}",
                    flush=True,
                )
        observation = record.get("observation") if record else None
        if isinstance(observation, dict) and observation.get("serving"):
            break
        time.sleep(3)
    else:
        dump_operation_logs(port, token, operations)
        raise TimeoutError(f"Local SearXNG never served within 300 seconds: {record}")
    configuration = record.get("configuration") if record else None
    search_port = configuration.get("port") if isinstance(configuration, dict) else None
    results = get_json(f"http://127.0.0.1:{search_port}/search?q=python&format=json")
    if not isinstance(results, dict) or "results" not in results:
        raise RuntimeError(f"SearXNG returned no result list: {str(results)[:300]}")
    print(f"Local SearXNG answered with {len(results['results'])} results", flush=True)


# Runs as an agent document tool would: the server's protection first, then the
# confined helper renders through Office and recalculates a formula.
_OFFICE_USE = """
import sys
from pathlib import Path
from clio_agent.runtime.execution_environment import protect_bundled_runtime
protect_bundled_runtime()
from docx import Document
from openpyxl import Workbook
from clio_agent.runtime.document_runtime import prepare_document_runtime, run_document_helper
workspace = Path(sys.argv[1])
document = Document()
document.add_paragraph("Rendering must not write bytecode into the app bundle.")
document.save(workspace / "seal.docx")
book = Workbook()
book.active.append([2, 3, "=SUM(A1:B1)"])
book.save(workspace / "seal.xlsx")
runtime = prepare_document_runtime(workspace)
for action, name in (("render", "seal.docx"), ("recalculate", "seal.xlsx")):
    result = run_document_helper(
        runtime, [action, str(workspace / name), str(workspace / "out")], cwd=workspace
    )
    if result.get("status") != "complete":
        raise SystemExit(f"{action} failed: {result}")
    print(f"Office {action} complete")
"""


def use_office(runtime: Path, root: Path, env: dict[str, str]) -> None:
    workspace = root / "documents"
    workspace.mkdir()
    subprocess.run(
        [str(runtime / "python/bin/python3.14"), "-I", "-B", "-X", "utf8", "-c", _OFFICE_USE]
        + [str(workspace)],
        env=env,
        check=True,
        timeout=600,
    )


def quarantine(app: Path) -> None:
    """Mark every file as downloaded by a browser, as a real DMG copy would be."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    value = f"0083;{int(time.time()):08x};Safari;".encode()
    for path in [app, *app.rglob("*")]:
        # XATTR_NOFOLLOW: mark links themselves, never what they point to.
        if libc.setxattr(os.fsencode(path), b"com.apple.quarantine", value, len(value), 0, 0x0001):
            raise OSError(ctypes.get_errno(), f"cannot quarantine {path}")


def gatekeeper_accepts(app: Path) -> bool:
    """Ask Gatekeeper as a first double-click would; print its full verdict."""
    result = subprocess.run(
        ["/usr/sbin/spctl", "--assess", "--type", "execute", "-vv", str(app)],
        capture_output=True,
        text=True,
    )
    print(f"spctl exit {result.returncode}: {(result.stdout + result.stderr).strip()}", flush=True)
    return result.returncode == 0


def check_runtime_tools(runtime: Path) -> None:
    """The bundled uvx must be uv's tool-runner shim, not a renamed copy of uv."""
    version = subprocess.run(
        [str(runtime / "bin/uvx"), "--version"], capture_output=True, text=True, check=True
    ).stdout
    if not version.startswith("uvx "):
        raise RuntimeError(f"Bundled uvx is not the uvx shim: {version.strip()!r}")


def stop(process: subprocess.Popen[bytes]) -> None:
    """Stop this smoke process and its owned children, bounded by ten seconds."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def launch_desktop(app: Path, root: Path, env: dict[str, str]) -> None:
    """Require the native Desktop process to survive startup against the backend."""
    with (root / "desktop.log").open("wb") as log:
        desktop = subprocess.Popen(
            [str(app / "Contents/MacOS/clio-desktop")],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            try:
                status = desktop.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Native Desktop remained running", flush=True)
            else:
                raise RuntimeError(f"Desktop exited during startup: {status}")
        finally:
            stop(desktop)
            print((root / "desktop.log").read_text(errors="replace"))


BUNDLED_GIT_PROGRAMS = (
    "git",
    "git-remote-https",
    "git-remote-http",
    "git-submodule",
    "git-sh-setup",
    "git-sh-i18n",
    "git-sh-i18n--envsubst",
)
SUBMODULE_URL = "https://github.com/iowarp/clio-agent-marketplace.git"


def check_bundled_git(git_core: Path, root: Path) -> None:
    """Require the app's own git to clone and check out submodules with no other git.

    A fresh Mac has only the developer-tools placeholder at /usr/bin/git, so
    nothing here may reach it. git runs with PATH=/var/empty; `git submodule` is
    a /bin/sh script, so it gets a PATH holding only the sed, basename and uname
    it uses (git puts its own exec-path first).
    """
    for name in BUNDLED_GIT_PROGRAMS:
        if not (git_core / name).is_file():
            raise ValueError(f"Bundled app has no {name}")
    git = str(git_core / "git")
    tools = root / "tools"
    tools.mkdir(parents=True)
    for tool in ("sed", "basename", "uname"):
        (tools / tool).symlink_to(f"/usr/bin/{tool}")
    home = root / "home"
    home.mkdir()
    base = {"HOME": str(home), "GIT_AUTHOR_NAME": "clio", "GIT_AUTHOR_EMAIL": "clio@localhost"}
    base |= {"GIT_COMMITTER_NAME": "clio", "GIT_COMMITTER_EMAIL": "clio@localhost"}

    def run(*args: str, path: str = "/var/empty", cwd: Path | None = None) -> str:
        return subprocess.run(
            [git, "-c", "init.defaultBranch=main", *args],
            env=base | {"PATH": path},
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout

    run("--version")
    head = run("ls-remote", SUBMODULE_URL, "HEAD").split("\t", 1)[0]
    if len(head) != 40:
        raise RuntimeError(f"Bundled git could not reach {SUBMODULE_URL}")
    super_dir = root / "super"
    run("init", "-q", str(super_dir))
    run("config", "-f", ".gitmodules", "submodule.mp.path", "mp", cwd=super_dir)
    run("config", "-f", ".gitmodules", "submodule.mp.url", SUBMODULE_URL, cwd=super_dir)
    run("add", ".gitmodules", cwd=super_dir)
    run("update-index", "--add", "--cacheinfo", f"160000,{head},mp", cwd=super_dir)
    run("commit", "-q", "-m", "super", cwd=super_dir)
    update = subprocess.run(
        [git, "submodule", "update", "--init", "--depth", "1"],
        env=base | {"PATH": str(tools)},
        cwd=super_dir,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = update.stdout + update.stderr
    if update.returncode or "not a git command" in output or "command not found" in output:
        raise RuntimeError(f"Bundled git submodule update failed:\n{output}")
    if run("rev-parse", "HEAD", cwd=super_dir / "mp").strip() != head:
        raise RuntimeError("Bundled git submodule checked out the wrong commit")
    print(f"Bundled git checked out a submodule at {head[:9]}", flush=True)


def smoke(app: Path, *, bundled: bool, exercise: bool = False) -> None:
    """Launch the native app and require readiness from the real bundled backend.

    ``exercise`` first runs ``--prepare-runtime``, then has the live backend serve web
    search and Office rendering, checking the seal after each stage.
    """
    verify(app)
    before = snapshot(app)
    # A SearXNG worker or install runs in its own session and may still be writing
    # under the agent home while the backend's stop settles.
    with tempfile.TemporaryDirectory(
        prefix="clio-macos-smoke-", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CLIO_", "GACT_"))
        }
        env.update(CLIO_AGENT_HOME=str(root / "agent"), CLIO_DESKTOP_HOME=str(root / "desktop"))
        if exercise:
            # The Desktop keys its data folder on HOME; keep it out of the real profile.
            (root / "home").mkdir()
            env["HOME"] = str(root / "home")
        if bundled:
            runtime = app / "Contents/Resources/gact-runtime"
            if not (runtime / "runtime.json").is_file():
                raise ValueError("Bundled app has no runtime manifest")
            if exercise:
                prepare_runtime(app, env)
                require_sealed(app, before, "--prepare-runtime")
            check_bundled_git(runtime / "git/libexec/git-core", root / "git")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            token = "macos-bundle-smoke"
            env["GACT_BUNDLED_RUNTIME_DIR"] = str(runtime)
            with (root / "backend.log").open("wb") as log:
                backend = subprocess.Popen(
                    [str(app / "Contents/MacOS/clio-agent"), "--port", str(port), "--token", token],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 90
                    request = Request(
                        f"http://127.0.0.1:{port}/v1/capabilities",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    while time.monotonic() < deadline:
                        if backend.poll() is not None:
                            raise RuntimeError(f"Packaged sidecar exited: {backend.returncode}")
                        try:
                            with urlopen(request, timeout=2) as response:
                                capabilities = json.load(response)
                            if not isinstance(capabilities, dict) or not capabilities:
                                raise ValueError("Backend returned empty capabilities")
                            print("Packaged sidecar served /v1/capabilities", flush=True)
                            break
                        except (URLError, TimeoutError):
                            time.sleep(1)
                    else:
                        raise TimeoutError(
                            "Packaged sidecar did not become ready within 90 seconds"
                        )
                    if exercise:
                        office_env = {**env, "GACT_BUNDLED_RUNTIME_DIR": str(runtime)}
                        use_web_search(port, token)
                        use_office(runtime, root, office_env)
                    # Attach to the live, real packaged backend. Launching Desktop
                    # first would start its own independently grouped backend and
                    # race this smoke's second server over the same configuration.
                    env["CLIO_GACT_URL"] = f"http://127.0.0.1:{port}"
                    launch_desktop(app, root, env)
                finally:
                    stop(backend)
                    print((root / "backend.log").read_text(errors="replace"))
        else:
            launch_desktop(app, root, env)
        # Starting Python must not invalidate the signed app by modifying resources.
        require_sealed(app, before, "the launch")


def main() -> None:
    """Run the macOS smoke against an app supplied by the build or installer."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("app", type=Path)
    parser.add_argument("--bundled", action="store_true")
    parser.add_argument(
        "--macho-list", type=Path, help="write the runtime's Mach-O inventory as JSON"
    )
    parser.add_argument(
        "--quarantine", action="store_true", help="mark the copy downloaded; assess Gatekeeper"
    )
    parser.add_argument(
        "--require-gatekeeper",
        action="store_true",
        help="fail unless Gatekeeper accepts the quarantined app (notarized builds)",
    )
    parser.add_argument(
        "--exercise",
        action="store_true",
        help="with --bundled: run --prepare-runtime, then use web search and Office",
    )
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    runtime = app / "Contents/Resources/gact-runtime"
    if args.bundled:
        check_runtime_tools(runtime)
        inventory = macho_inventory(runtime)
        print(
            f"Mach-O in gact-runtime: {len(inventory['files'])} files, "
            f"{len(inventory['archived'])} inside archives",
            flush=True,
        )
        for member in inventory["archived"]:
            print(f"::warning::unsignable Mach-O inside an archive: {member}", flush=True)
        if args.macho_list:
            args.macho_list.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    smoke(app, bundled=args.bundled, exercise=args.exercise and args.bundled)
    if args.quarantine or args.require_gatekeeper:
        quarantine(app)
        if gatekeeper_accepts(app):
            smoke(app, bundled=args.bundled)
        elif args.require_gatekeeper:
            raise RuntimeError("Gatekeeper rejected the quarantined app")
        else:
            print("::notice::Gatekeeper rejects this unnotarized build; quarantined boot skipped")


if __name__ == "__main__":
    main()
