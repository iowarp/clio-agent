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
from urllib.error import URLError
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


def require_unchanged(app: Path, before: dict[str, tuple[int, int]]) -> None:
    after = snapshot(app)
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if changed:
        raise RuntimeError(
            f"Running the app wrote into its bundle ({len(changed)} paths): {changed[:20]}"
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


def smoke(app: Path, *, bundled: bool) -> None:
    """Launch the native app and require readiness from the real bundled backend."""
    verify(app)
    before = snapshot(app)
    with tempfile.TemporaryDirectory(prefix="clio-macos-smoke-") as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CLIO_", "GACT_"))
        }
        env.update(CLIO_AGENT_HOME=str(root / "agent"), CLIO_DESKTOP_HOME=str(root / "desktop"))
        if bundled:
            runtime = app / "Contents/Resources/gact-runtime"
            if not (runtime / "runtime.json").is_file():
                raise ValueError("Bundled app has no runtime manifest")
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
        require_unchanged(app, before)
        verify(app)


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
    smoke(app, bundled=args.bundled)
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
