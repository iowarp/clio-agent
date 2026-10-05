"""Boot an installed Linux/Windows Desktop and its actual packaged backend.

The installer/extractor must run first. No development server or replacement
runtime is supplied: Desktop owns runtime extraction, launch and authentication.
This checks startup, not visual rendering, inference or an updater installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import psutil


def backend_connection(process: psutil.Process) -> tuple[str, str] | None:
    """Read only the launched descendant's endpoint and transient auth credential."""
    args = process.cmdline()
    if "clio_agent.gact" not in args or "--port" not in args:
        return None
    port = int(args[args.index("--port") + 1])
    token = process.environ().get("CLIO_AUTH_TOKEN", "")
    if not token or not 0 < port < 65536:
        return None
    return f"http://127.0.0.1:{port}", token


def stop_owned(process: psutil.Process, observed: list[psutil.Process]) -> None:
    """Stop the launched Desktop and its current descendants, never host-wide services."""
    try:
        children = process.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    owned = list({item.pid: item for item in [process, *reversed(children), *observed]}.values())
    for child in owned:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(owned, timeout=10)
    for child in alive:
        child.kill()
    _, alive = psutil.wait_procs(alive, timeout=5)
    if alive:
        raise RuntimeError("Packaged process cleanup did not finish")


def file_sha256(path: Path) -> str:
    """Hash the installed Desktop executable for the qualification receipt."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_bundled_python(process: psutil.Process) -> str:
    """Reject fallback installations or a development interpreter masquerading as a package."""
    directory = process.environ().get("GACT_BUNDLED_RUNTIME_DIR", "")
    if not directory:
        raise ValueError("Desktop backend did not inherit a bundled runtime directory")
    runtime = Path(directory).resolve(strict=True)
    manifest = json.loads((runtime / "runtime.json").read_text(encoding="utf-8"))
    expected = (runtime / manifest["exec"][0]).resolve(strict=True)
    actual = Path(process.exe()).resolve(strict=True)
    if not expected.is_relative_to(runtime) or actual != expected:
        raise ValueError("Desktop launched an interpreter outside its packaged runtime")
    return str(actual)


def smoke(desktop: Path, evidence: Path, *, timeout: int = 180) -> dict[str, Any]:
    """Require a real child backend, authenticated capabilities and surviving Desktop."""
    desktop = desktop.resolve(strict=True)
    evidence.mkdir(parents=True, exist_ok=False)
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("CLIO_", "GACT_"))
    }
    env.update(
        CLIO_AGENT_HOME=str(evidence / "agent"),
        CLIO_DESKTOP_HOME=str(evidence / "desktop-state"),
        CLIO_RUNTIME_STATE_DIR=str(evidence / "core-supervision"),
        CLIO_ENV_FILE_LOADED="1",
        # Prevent attach-first from finding any unrelated CLIO on the test host.
        CLIO_GACT_URL="http://127.0.0.1:9",
    )
    tokens: set[str] = set()
    observed: dict[int, psutil.Process] = {}
    log_path = evidence / "desktop.log"
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            [str(desktop)],
            cwd=evidence,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process = psutil.Process(child.pid)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"Desktop exited during startup ({child.returncode})")
                for candidate in process.children(recursive=True):
                    observed[candidate.pid] = candidate
                    try:
                        connection = backend_connection(candidate)
                        if connection is None:
                            continue
                        url, token = connection
                        tokens.add(token)
                        request = Request(
                            url + "/v1/capabilities", headers={"Authorization": f"Bearer {token}"}
                        )
                        with urlopen(request, timeout=2) as response:
                            capabilities = json.load(response)
                        if not isinstance(capabilities, dict) or not capabilities.get(
                            "contract_version"
                        ):
                            raise ValueError("Packaged backend returned invalid capabilities")
                        executable = require_bundled_python(candidate)
                    except (psutil.NoSuchProcess, URLError, TimeoutError):
                        continue
                    # Readiness alone can precede an immediate native WebView crash.
                    try:
                        status = child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        result = {
                            "status": "passed",
                            "system": platform.platform(),
                            "desktop": str(desktop),
                            "backend_python": executable,
                            "desktop_sha256": file_sha256(desktop),
                            "contract_version": capabilities["contract_version"],
                            "backend_owned_by_desktop": True,
                            "inference_performed": False,
                            "updater_install_qualified": False,
                        }
                        return result
                    raise RuntimeError(f"Desktop exited after backend readiness ({status})")
                time.sleep(1)
            raise TimeoutError(f"Desktop did not launch its packaged backend within {timeout}s")
        finally:
            try:
                stop_owned(process, list(observed.values()))
            finally:
                log.close()
                sanitized = log_path.read_text(errors="replace")
                for token in tokens:
                    sanitized = sanitized.replace(token, "[redacted]")
                log_path.write_text(sanitized, encoding="utf-8")
    # The caller retains the directory on both success and failure.


def main() -> None:
    """Run against one installed executable and retain sanitized startup evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("desktop", type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    result = smoke(args.desktop, args.evidence_dir.resolve())
    text = json.dumps(result, indent=2)
    (args.evidence_dir / "receipt.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
