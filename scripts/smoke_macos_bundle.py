#!/usr/bin/env python3
"""Verify and launch the actual macOS app, then boot its packaged sidecar.

Run against a relocated copy from a mounted DMG or extracted updater archive.
This tests bundle integrity and runtime compatibility, not Apple notarization.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def verify(app: Path) -> None:
    """Require an intact application seal and an executable for this host."""
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        metadata = plistlib.load(stream)
    if metadata["CFBundleIdentifier"] != "ai.iowarp.clio.desktop":
        raise ValueError("Unexpected application identity")
    subprocess.run(["file", str(app / "Contents/MacOS/clio-desktop")], check=True)


def stop(process: subprocess.Popen[bytes]) -> None:
    """Stop this smoke process and its owned children, bounded by ten seconds."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def smoke(app: Path, *, bundled: bool) -> None:
    """Launch the native app and require readiness from the real bundled backend."""
    verify(app)
    with tempfile.TemporaryDirectory(prefix="clio-macos-smoke-") as directory:
        root = Path(directory)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CLIO_", "GACT_"))
        }
        env.update(CLIO_AGENT_HOME=str(root / "agent"), CLIO_DESKTOP_HOME=str(root / "desktop"))
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
                finally:
                    stop(backend)
                    print((root / "backend.log").read_text(errors="replace"))
        # Starting Python must not invalidate the signed app by modifying resources.
        verify(app)


def main() -> None:
    """Run the macOS smoke against an app supplied by the build or installer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--bundled", action="store_true")
    args = parser.parse_args()
    smoke(args.app.resolve(strict=True), bundled=args.bundled)


if __name__ == "__main__":
    main()
