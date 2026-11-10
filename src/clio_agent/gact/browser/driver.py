"""Playwright's Node.js driver, fetched on first use when the desktop bundle leaves it out.

The ``playwright`` wheel carries the official Node.js binary its driver runs on
(``playwright/driver/node``, 122 MB unpacked, 28 MB of the macOS download). The desktop
bundles drop it at build time (``install/stage_playwright_node.py``) and write
``playwright/driver/clio-node.json`` in its place: the nodejs.org archive that contains the
byte-identical binary, the archive's SHA-256 and the binary's SHA-256 (the hash of the file
the hash-locked wheel installed). The first browser use fetches that archive, checks both
hashes, checks that the binary runs, and only then moves it into CLIO's cache
(``<cache>/browser/node-<version>-<hash>``) in one rename, so an interrupted or failed fetch
leaves nothing that looks installed. Failed attempts are retried; when every attempt fails,
the error is a typed ``browser_runtime_install_failed`` and the next browser use starts over.

Playwright is pointed at the binary with its own ``PLAYWRIGHT_NODEJS_PATH`` setting. An
install with the wheel's binary in place (pip installs, unstripped bundles) never fetches,
and a ``PLAYWRIGHT_NODEJS_PATH`` the person set is always honoured.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.gact.browser.settings import BrowserError
from clio_agent.runtime import package_preparation, trace

__all__ = [
    "MANIFEST",
    "ENV",
    "driver_for_action",
    "ensure_node",
    "fetch_state",
    "node_path",
    "preparing_answer",
]

MANIFEST = "clio-node.json"
ENV = "PLAYWRIGHT_NODEJS_PATH"
REASON_PREPARING = "browser_runtime_preparing"
REASON_INSTALL_FAILED = "browser_runtime_install_failed"
#: Attempts per fetch, and the pause before each retry.
_ATTEMPTS = 3
_RETRY_PAUSE_S = (2.0, 5.0)
#: Seconds a read may stall before the attempt counts as failed.
_READ_TIMEOUT_S = 30.0
_CHUNK = 1 << 20
_COMPLETE = "INSTALLATION_COMPLETE"

#: What one failed attempt can raise; each is retried.
_ATTEMPT_ERRORS = (
    OSError,
    ValueError,
    http.client.HTTPException,
    subprocess.SubprocessError,
    tarfile.TarError,
    zipfile.BadZipFile,
)

_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
#: This process's fetch while it runs: attempt, bytes and total, or the last attempt's error.
_state: dict[str, Any] = {}
_TASK_LOCK = threading.Lock()
_task: concurrent.futures.Future[Path] | None = None


def _executable_name() -> str:
    return "node.exe" if sys.platform == "win32" else "node"


def driver_dir() -> Path | None:
    """The installed ``playwright/driver`` folder, found without importing Playwright."""
    spec = importlib.util.find_spec("playwright")
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    return Path(locations[0]) / "driver" if locations else None


def manifest() -> dict[str, Any] | None:
    """The bundle's record of the Node.js archive to fetch, or ``None`` when it has none."""
    folder = driver_dir()
    if folder is None:
        return None
    try:
        record = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise BrowserError(REASON_INSTALL_FAILED, f"{MANIFEST} is unreadable: {exc}") from exc
    required = ("node_version", "url", "sha256", "size", "member", "node_sha256")
    if not isinstance(record, dict) or any(not record.get(key) for key in required):
        raise BrowserError(REASON_INSTALL_FAILED, f"{MANIFEST} is incomplete")
    return record


def _bundled() -> Path | None:
    folder = driver_dir()
    candidate = folder / _executable_name() if folder is not None else None
    return candidate if candidate is not None and candidate.is_file() else None


def _root(record: dict[str, Any]) -> Path:
    tag = f"node-{record['node_version']}-{str(record['node_sha256'])[:12]}"
    return paths.user_cache_dir() / "browser" / tag


def _fetched(record: dict[str, Any]) -> Path | None:
    root = _root(record)
    node = root / _executable_name()
    return node if (root / _COMPLETE).is_file() and node.is_file() else None


def _use(node: Path) -> Path:
    # Playwright reads this when its driver starts (playwright._impl._driver).
    os.environ[ENV] = str(node)
    return node


def node_path() -> Path | None:
    """The Node.js binary Playwright will run, or ``None`` while it still has to be fetched."""
    chosen = os.environ.get(ENV)
    if chosen:
        return Path(chosen)
    bundled = _bundled()
    if bundled is not None:
        return bundled
    record = manifest()
    return _fetched(record) if record is not None else None


def fetch_state() -> dict[str, Any]:
    """This process's fetch progress (``attempt``, ``bytes``, ``total``, ``error``), if any."""
    with _STATE_LOCK:
        return dict(_state)


def _set_state(**values: Any) -> None:
    with _STATE_LOCK:
        _state.update(values)


def _megabytes(count: int) -> str:
    return f"{count / 1_000_000:.0f}"


def _preparing() -> dict[str, Any]:
    state = fetch_state()
    total = int(state.get("total") or 0)
    done = int(state.get("bytes") or 0)
    attempt = int(state.get("attempt") or 1)
    if state.get("phase") == "checking":
        progress = ": unpacking and checking it"
    else:
        progress = f": {_megabytes(done)} of {_megabytes(total)} MB" if total else ""
    retry = (
        f" (attempt {attempt} of {_ATTEMPTS}; the last one failed: {state['error']})"
        if attempt > 1 and state.get("error")
        else ""
    )
    return {
        "status": "preparing",
        "reason": REASON_PREPARING,
        "progress": {
            "step": "browser_driver",
            "phase": state.get("phase") or "downloading",
            "bytes": done,
            "total": total,
            "attempt": attempt,
        },
        "message": (
            "The browser's Node.js driver is being downloaded for this computer "
            f"(first use only){progress}{retry}. Try again in a few seconds."
        ),
    }


def _fetch_record_root() -> Path:
    return paths.user_cache_dir() / "browser" / "node-fetch"


def preparing_answer() -> dict[str, Any] | None:
    """A typed "still downloading" answer while another process fetches the driver."""
    record = package_preparation.current(_fetch_record_root())
    if record is None or record.get("state") != "preparing":
        return None
    if int(record.get("pid") or 0) == os.getpid():
        return None
    return _preparing()


def _download(record: dict[str, Any], archive: Path) -> None:
    expected = int(record["size"])
    digest = hashlib.sha256()
    done = 0
    _set_state(bytes=0, total=expected)
    request = urllib.request.Request(  # noqa: S310 - https URL pinned in the bundle manifest
        str(record["url"]), headers={"User-Agent": "clio-agent"}
    )
    with urllib.request.urlopen(request, timeout=_READ_TIMEOUT_S) as answer:  # noqa: S310
        with archive.open("wb") as out:
            while chunk := answer.read(_CHUNK):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                _set_state(bytes=done)
                if done > expected:
                    raise ValueError(f"the download is larger than the {expected} bytes expected")
    if done != expected:
        raise ValueError(f"the download stopped at {done} of {expected} bytes")
    if digest.hexdigest() != record["sha256"]:
        raise ValueError("the downloaded archive does not match its recorded SHA-256")


def _extract(record: dict[str, Any], archive: Path, node: Path) -> None:
    member = str(record["member"])
    digest = hashlib.sha256()
    if str(record["url"]).endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle, bundle.open(member) as source:
            _copy(source, node, digest)
    else:
        with tarfile.open(archive, mode="r:*") as bundle:
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"{member} is not a file in the archive")
            with source:
                _copy(source, node, digest)
    if digest.hexdigest() != record["node_sha256"]:
        raise ValueError("Node.js in the archive is not the binary this Playwright release ships")
    node.chmod(0o755)


def _copy(source: Any, node: Path, digest: Any) -> None:
    with node.open("wb") as out:
        while chunk := source.read(_CHUNK):
            out.write(chunk)
            digest.update(chunk)


def _check_runs(record: dict[str, Any], node: Path) -> None:
    done = subprocess.run(  # noqa: S603 - the binary just verified against its pinned hash
        [str(node), "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    version = (done.stdout or "").strip()
    if done.returncode != 0 or version != f"v{record['node_version']}":
        tail = (done.stderr or done.stdout or "").strip()[-400:]
        raise ValueError(f"Node.js did not start (exit {done.returncode}): {tail}")


def _attempt(record: dict[str, Any]) -> Path:
    root = _root(record)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".{root.name}.{os.getpid()}.{threading.get_ident()}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        archive = staging / "archive"
        node = staging / _executable_name()
        _download(record, archive)
        _set_state(phase="checking")
        _extract(record, archive, node)
        archive.unlink()
        _check_runs(record, node)
        (staging / _COMPLETE).write_text(str(record["node_version"]), encoding="utf-8")
        if root.exists():  # a leftover without the completion marker
            shutil.rmtree(root)
        staging.replace(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    found = _fetched(record)
    if found is None:
        raise ValueError(f"Node.js is not where it was moved ({root})")
    return found


def ensure_node() -> Path | None:
    """Make Playwright's Node.js driver available, fetching it on the first call.

    Returns the binary, or ``None`` when Playwright is not installed (it reports that
    itself). Blocks for the fetch; :func:`driver_for_action` is the non-blocking form.

    Raises:
        BrowserError: ``browser_runtime_preparing`` while another process fetches it,
            ``browser_runtime_install_failed`` when every attempt failed.
    """
    with _LOCK:
        chosen = os.environ.get(ENV)
        if chosen:
            return Path(chosen)
        if _bundled() is not None:
            return _bundled()
        record = manifest()
        if record is None:
            return None
        found = _fetched(record)
        if found is not None:
            return _use(found)
        other = preparing_answer()
        if other is not None:
            raise BrowserError(REASON_PREPARING, other["message"], status=other["status"])
        trace.event("BROWSER", "node driver fetch starting url=%s", record["url"])
        _set_state(attempt=1, bytes=0, total=int(record["size"]), error="")
        try:
            with package_preparation.preparing(_fetch_record_root()):
                for attempt in range(1, _ATTEMPTS + 1):
                    _set_state(attempt=attempt, bytes=0, phase="downloading")
                    started = time.monotonic()
                    try:
                        found = _attempt(record)
                    except _ATTEMPT_ERRORS as exc:
                        error = f"{type(exc).__name__}: {exc}"[-400:]
                        _set_state(error=error)
                        trace.event(
                            "BROWSER",
                            "node driver fetch attempt=%d failed error=%s",
                            attempt,
                            error,
                        )
                        if attempt == _ATTEMPTS:
                            raise BrowserError(
                                REASON_INSTALL_FAILED,
                                "downloading the browser's Node.js driver failed after "
                                f"{_ATTEMPTS} attempts ({error}); the next browser use tries again",
                            ) from exc
                        time.sleep(_RETRY_PAUSE_S[min(attempt, len(_RETRY_PAUSE_S)) - 1])
                        continue
                    trace.event(
                        "BROWSER",
                        "node driver fetched path=%s seconds=%.1f",
                        found,
                        time.monotonic() - started,
                    )
                    return _use(found)
        finally:
            with _STATE_LOCK:
                _state.clear()
    raise AssertionError("unreachable")  # pragma: no cover


def _fetch_in_background(task: concurrent.futures.Future[Path]) -> None:
    global _task
    found: Path | None = None
    error: BaseException | None = None
    try:
        found = ensure_node()
    except BrowserError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - forwarded to the waiting action, typed
        error = BrowserError(REASON_INSTALL_FAILED, f"fetching the browser's driver failed: {exc}")
    finally:
        # Forget the task before completing it, so a use that sees it fail starts afresh.
        with _TASK_LOCK:
            if _task is task:
                _task = None
        if error is None and found is None:
            error = BrowserError(REASON_INSTALL_FAILED, "the driver fetch stopped unexpectedly")
        if error is not None:
            task.set_exception(error)
        elif found is not None:
            task.set_result(found)


def driver_for_action(wait_s: float) -> None:
    """Make the driver available for a browser action, waiting at most ``wait_s`` for it.

    A fetch outlives the action that started it; while it runs, the action gets a typed
    ``browser_runtime_preparing`` naming the bytes so far, and a later action finds it done.

    Raises:
        BrowserError: ``browser_runtime_preparing`` while the fetch runs,
            ``browser_runtime_install_failed`` when it failed.
    """
    global _task
    if os.environ.get(ENV) or _bundled() is not None:
        return
    record = manifest()
    if record is None:
        return
    found = _fetched(record)
    if found is not None:  # fetched earlier, possibly by another process
        _use(found)
        return
    with _TASK_LOCK:
        task = _task
        if task is None:
            task = concurrent.futures.Future()
            _task = task
            threading.Thread(
                target=_fetch_in_background,
                args=(task,),
                name="clio-browser-node-fetch",
                daemon=True,
            ).start()
    try:
        task.result(timeout=max(0.0, wait_s))
    except concurrent.futures.TimeoutError:
        answer = _preparing()
        raise BrowserError(
            REASON_PREPARING,
            answer["message"],
            status=answer["status"],
            progress=answer["progress"],
        ) from None
