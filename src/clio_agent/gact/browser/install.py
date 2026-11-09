"""Playwright and its Chromium build: imported and downloaded on first use only.

The Python ``playwright`` package is a clio-agent dependency, but nothing imports it until
the browser service starts, as h5py waits for the first HDF5 request. The Chromium build
matching that Playwright release is downloaded by Playwright's own installer into CLIO's
cache (``<cache>/browser/playwright-<version>``), never at install time, so the base install
and the desktop bundle do not grow. The download is recorded with
:func:`clio_agent.runtime.package_preparation.preparing`, so a second process sees it
running instead of racing it. CLIO never runs ``playwright install-deps`` (it needs root);
missing system libraries are a typed ``browser_runtime_missing_libraries`` naming them.

The executable is located under the install folder and passed to Chromium's launch, so the
server's own environment (``PLAYWRIGHT_BROWSERS_PATH``) is never changed for its children.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import subprocess
import threading
from importlib import metadata
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import package_preparation, trace

__all__ = [
    "REASON_INSTALL_FAILED",
    "REASON_MISSING_LIBRARIES",
    "REASON_PREPARING",
    "browsers_root",
    "chromium_executable",
    "chromium_for_action",
    "ensure_chromium",
    "missing_libraries",
    "playwright_version",
    "preparing_answer",
    "probe_browser",
    "record_sandbox",
]

REASON_PREPARING = "browser_runtime_preparing"
REASON_INSTALL_FAILED = "browser_runtime_install_failed"
REASON_MISSING_LIBRARIES = "browser_runtime_missing_libraries"
REASON_NOT_INSTALLED = "browser_runtime_not_installed"
#: Seconds the Chromium download may take before it counts as failed.
_INSTALL_TIMEOUT_S = 1800
_LOCK = threading.Lock()
#: The executable's name inside a Chrome for Testing folder, by platform.
_EXECUTABLES = ("chrome", "chrome.exe", "Google Chrome for Testing", "Chromium")


def playwright_version() -> str:
    """The installed Playwright release, or ``""`` when the package is missing."""
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:
        return ""


def browsers_root() -> Path:
    """Where this Playwright release's browsers live in CLIO's cache."""
    return paths.user_cache_dir() / "browser" / f"playwright-{playwright_version() or 'none'}"


def _driver() -> tuple[str, str]:
    from playwright._impl._driver import compute_driver_executable  # noqa: PLC0415

    node, cli = compute_driver_executable()
    return str(node), str(cli)


def _browsers_json() -> Path:
    """Playwright's ``browsers.json``, found without importing the package."""
    spec = importlib.util.find_spec("playwright")
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    if not locations:
        raise BrowserError(REASON_NOT_INSTALLED, "the playwright package is not installed")
    return Path(locations[0]) / "driver" / "package" / "browsers.json"


def chromium_revision() -> str:
    """The Chromium revision this Playwright release pins (from its ``browsers.json``)."""
    listing = json.loads(_browsers_json().read_text(encoding="utf-8"))
    for entry in listing.get("browsers") or ():
        if entry.get("name") == "chromium":
            return str(entry.get("revision") or "")
    raise BrowserError(REASON_INSTALL_FAILED, "this Playwright release names no Chromium build")


def chromium_executable(root: Path | None = None) -> Path | None:
    """The installed Chromium's executable, or ``None`` when it is not installed."""
    if not playwright_version():
        return None
    folder = (root or browsers_root()) / f"chromium-{chromium_revision()}"
    if not (folder / "INSTALLATION_COMPLETE").exists():
        return None
    for name in _EXECUTABLES:
        for candidate in sorted(folder.rglob(name)):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _install_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"}
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    return env


def ensure_chromium() -> Path:
    """The Chromium executable, downloading it on the first call.

    Raises:
        BrowserError: ``browser_runtime_preparing`` while another process downloads it,
            ``browser_runtime_install_failed`` when the download fails.
    """
    root = browsers_root()
    with _LOCK:
        found = chromium_executable(root)
        if found is not None:
            return found
        other = preparing_answer()
        if other is not None:
            raise BrowserError(REASON_PREPARING, other["message"], status=other["status"])
        node, cli = _driver()
        trace.event("BROWSER", "chromium install starting root=%s", root)
        with package_preparation.preparing(root):
            done = subprocess.run(  # noqa: S603 - Playwright's own pinned installer
                [node, cli, "install", "--no-shell", "chromium"],
                env=_install_env(root),
                capture_output=True,
                text=True,
                timeout=_INSTALL_TIMEOUT_S,
                check=False,
            )
            if done.returncode != 0:
                tail = (done.stderr or done.stdout or "").strip()[-1500:]
                raise BrowserError(
                    REASON_INSTALL_FAILED,
                    f"downloading Chromium failed (exit {done.returncode}): {tail}",
                )
        found = chromium_executable(root)
        if found is None:
            raise BrowserError(REASON_INSTALL_FAILED, f"Chromium is not where it went ({root})")
        trace.event("BROWSER", "chromium installed path=%s", found)
        return found


_DOWNLOAD_LOCK = threading.Lock()
#: This process's Chromium download, while it runs.
_download: concurrent.futures.Future[Path] | None = None


def _download_in_background(task: concurrent.futures.Future[Path]) -> None:
    global _download
    error: BaseException | None = None
    try:
        found = ensure_chromium()
    except BaseException as exc:  # noqa: BLE001 - handed to whoever waits on the task
        error = exc
    # Forget the task before completing it, so an action that sees it fail starts afresh.
    with _DOWNLOAD_LOCK:
        if _download is task:
            _download = None
    if error is not None:
        task.set_exception(error)
    else:
        task.set_result(found)


def chromium_for_action(wait_s: float) -> Path:
    """The Chromium executable for a browser action, waiting at most ``wait_s`` for it.

    The first use downloads Chromium, which can take minutes, longer than one action's
    ``browser.action_timeout_s``. The download runs on its own thread and outlives the
    action; while it runs, the action gets ``browser_runtime_preparing`` instead of a
    timeout, and a later action finds the browser installed (iowarp/clio-agent#1975).

    Raises:
        BrowserError: ``browser_runtime_preparing`` while the download runs,
            ``browser_runtime_install_failed`` when it failed.
    """
    global _download
    with _DOWNLOAD_LOCK:
        task = _download
        if task is None:
            task = concurrent.futures.Future()
            _download = task
            threading.Thread(
                target=_download_in_background,
                args=(task,),
                name="clio-browser-install",
                daemon=True,
            ).start()
    try:
        return task.result(timeout=max(0.0, wait_s))
    except concurrent.futures.TimeoutError:
        answer = _preparing()
        raise BrowserError(REASON_PREPARING, answer["message"], status=answer["status"]) from None


def _preparing() -> dict[str, Any]:
    return {
        "status": "preparing",
        "reason": REASON_PREPARING,
        "message": (
            "The browser is being downloaded for this computer (first use only). "
            "Try again in a minute."
        ),
    }


def preparing_answer() -> dict[str, Any] | None:
    """A typed "still downloading" answer while another process downloads Chromium."""
    record = package_preparation.current(browsers_root())
    if record is None or record.get("state") != "preparing":
        return None
    if int(record.get("pid") or 0) == os.getpid():
        return None
    return _preparing()


_MISSING = re.compile(r"(lib[\w.+-]+\.so[\w.]*)")


def missing_libraries(error_text: str) -> list[str]:
    """The system libraries a failed launch says are missing (empty when none are)."""
    if "missing dependencies" not in error_text and "shared libraries" not in error_text:
        return []
    return sorted(set(_MISSING.findall(error_text)))


def _sandbox_path() -> Path:
    return paths.user_cache_dir() / "browser" / "sandbox.json"


def record_sandbox(*, sandboxed: bool, reason: str) -> None:
    """Remember the last launch's sandbox state for the doctor row."""
    path = _sandbox_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sandboxed": sandboxed, "reason": reason}), encoding="utf-8")
    except OSError as exc:
        trace.event("BROWSER", "browser_sandbox_record_unwritable error=%s", exc)


def _last_sandbox() -> dict[str, Any]:
    try:
        value = json.loads(_sandbox_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def probe_browser() -> Any:
    """The ``browser`` doctor row: version, cache path and sandbox state (not required)."""
    from clio_agent.runtime.status import IntegrationState, IntegrationStatus  # noqa: PLC0415

    version = playwright_version()
    root = browsers_root()
    sandbox = _last_sandbox()
    details: dict[str, Any] = {"playwright": version, "cache": str(root), "sandbox": sandbox}
    if not version:
        state, summary = IntegrationState.UNAVAILABLE, "The playwright package is not installed."
        next_action = "Reinstall clio-agent with its dependencies."
    else:
        executable = chromium_executable(root)
        details["executable"] = str(executable or "")
        if executable is None:
            record = package_preparation.current(root) or {}
            state = IntegrationState.SKIPPED
            summary = (
                "The browser is being downloaded."
                if record.get("state") == "preparing"
                else "The browser is downloaded the first time the agent opens a page."
            )
            next_action = "None."
        elif sandbox.get("sandboxed") is False:
            state = IntegrationState.DEGRADED
            summary = f"The browser runs with Chromium's sandbox off: {sandbox.get('reason')}"
            next_action = "Run CLIO as a normal user with unprivileged user namespaces."
        else:
            state, summary, next_action = (
                IntegrationState.READY,
                f"Chromium for Playwright {version} is installed.",
                "None.",
            )
    return IntegrationStatus(
        name="browser",
        state=state,
        summary=summary,
        config_source="runtime:browser",
        next_action=next_action,
        details=details,
        required=False,
    )
