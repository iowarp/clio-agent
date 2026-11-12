"""Playwright and its Chromium build: imported on first use, downloaded at install time.

The Python ``playwright`` package is a clio-agent dependency, but nothing imports it until
the browser service starts, as h5py waits for the first HDF5 request. The Chromium build
matching that Playwright release is downloaded by Playwright's own installer into CLIO's
cache (``<cache>/browser/playwright-<version>``) by the installer's package step
(:func:`prepare_at_install`, beside the other packages), so the desktop bundle does not grow
and the first browser use does not wait for it. A browser use that still finds it missing
(the install-time fetch failed, or an install predating it) downloads it then. When the
runtime pins the Chromium archive (``clio-node.json``, written at build time), CLIO
downloads it itself, keeps only bytes matching its SHA-256 and unpacks it on several
threads while Playwright fetches its small helpers; Playwright's install then finds it in
place. The download is recorded with
:func:`clio_agent.runtime.package_preparation.preparing`, so a second process sees it
running instead of racing it. CLIO never runs ``playwright install-deps`` (it needs root);
missing system libraries are a typed ``browser_runtime_missing_libraries`` naming them.

The executable is located under the install folder and passed to Chromium's launch, so the
server's own environment (``PLAYWRIGHT_BROWSERS_PATH``) is never changed for its children.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import threading
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent import paths
from clio_agent.gact.browser import driver
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
    "install_failure",
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
#: Seconds the pinned Chromium download may wait for its next bytes.
_READ_TIMEOUT_S = 60
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

    # A desktop bundle fetches the driver's Node.js on first use (browser/driver.py).
    driver.ensure_node()
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


def _chromium_pin() -> dict[str, Any] | None:
    """The Chromium archive a bundled runtime pins for this Playwright release, if any.

    ``install/stage_playwright_node.py`` records it in ``clio-node.json`` at build time.
    """
    try:
        pin = (driver.manifest() or {}).get("chromium")
        if not isinstance(pin, dict) or str(pin.get("revision")) != chromium_revision():
            return None
        if not str(pin.get("url", "")).startswith("https://") or not pin.get("sha256"):
            return None
        return pin
    except (BrowserError, OSError, ValueError):
        return None


def _fetch_pinned(pin: dict[str, Any], root: Path) -> Path:
    """Download the pinned archive into ``root``; only bytes matching its SHA-256 are kept."""
    url = str(pin["url"])
    archive = root / f".download-{url.rsplit('/', 1)[-1]}"
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "clio-agent"})
    try:
        with (
            urllib.request.urlopen(request, timeout=_READ_TIMEOUT_S) as answer,  # noqa: S310
            archive.open("wb") as out,
        ):
            while chunk := answer.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != str(pin["sha256"]):
            raise ValueError("the Chromium archive does not match its recorded SHA-256")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return archive


def _make_link(staging: Path, name: str, pointed: str) -> None:
    """The archive's symbolic link ``name`` -> ``pointed``, refused unless it stays inside."""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), pointed))
    if (
        not name
        or posixpath.isabs(name)
        or posixpath.isabs(pointed)
        or resolved == ".."
        or resolved.startswith("../")
        or ".." in PurePosixPath(name).parts
    ):
        raise ValueError(f"the Chromium archive's link {name!r} -> {pointed!r} leaves it")
    link = staging / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(pointed)


def _unpack_chromium(archive: Path, root: Path, revision: str) -> bool:
    """Install Chromium from ``archive`` the way Playwright's installer does, in parallel.

    Playwright extracts its 300-file archive on one thread (about 21 s on Windows under
    Defender); this does the same extraction on several. The folder is filled under a name
    Playwright's cleanup ignores, gets Playwright's ``INSTALLATION_COMPLETE`` marker, and
    only then takes its final name, so Playwright never sees it unfinished and its own
    ``install`` afterwards finds Chromium installed. The macOS app bundle's symbolic links
    (its framework's ``Versions/Current`` and the links through it) are made last, each
    checked to stay inside the folder. On Windows, an archive with links is left to
    Playwright: ``False``.
    """
    with zipfile.ZipFile(archive) as listing:
        entries = listing.infolist()
        links = [e for e in entries if stat.S_ISLNK(e.external_attr >> 16)]
        targets = {e.filename.rstrip("/"): listing.read(e).decode("utf-8") for e in links}
    if links and os.name == "nt":
        return False
    target = root / f"chromium-{revision}"
    staging = root / f".unpack-chromium-{revision}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    linked = {e.filename for e in links}
    files = sorted(
        (e for e in entries if not e.is_dir() and e.filename not in linked),
        key=lambda e: -e.file_size,
    )
    inside = staging.resolve()
    for entry in entries:  # folders first: 3.12's zipfile makes them without exist_ok (a race)
        if entry.filename in linked:
            continue
        path = staging / entry.filename
        folder = (path if entry.is_dir() else path.parent).resolve()
        if folder.is_relative_to(inside):
            folder.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(8, os.cpu_count() or 1))
    shares: list[list[zipfile.ZipInfo]] = [[] for _ in range(workers)]
    loads = [0] * workers
    for entry in files:  # the largest file first, each to the least loaded worker
        least = loads.index(min(loads))
        shares[least].append(entry)
        loads[least] += entry.file_size

    def _extract(share: list[zipfile.ZipInfo]) -> None:
        with zipfile.ZipFile(archive) as source:
            for entry in share:
                written = Path(source.extract(entry, staging))
                mode = (entry.external_attr >> 16) & 0o777
                if mode and os.name != "nt":
                    written.chmod(mode)

    try:
        with concurrent.futures.ThreadPoolExecutor(workers, "clio-chromium-unpack") as pool:
            for done in [pool.submit(_extract, share) for share in shares if share]:
                done.result()
        for name, pointed in sorted(targets.items()):
            _make_link(staging, name, pointed)
        (staging / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return True


def _run_install(
    node: str, cli: str, root: Path, names: tuple[str, ...] = ("chromium",)
) -> subprocess.CompletedProcess:
    env = _install_env(root)
    if names != ("chromium",):
        # Playwright's cleanup lists its folder after deciding what is in use; run beside
        # the unpack, it could take the newly renamed Chromium folder for unused.
        env["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
    return subprocess.run(  # noqa: S603 - Playwright's own pinned installer
        [node, cli, "install", "--no-shell", *names],
        env=env,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
        check=False,
    )


def _install_pinned(pin: dict[str, Any], node: str, cli: str, root: Path) -> None:
    """Install the pinned Chromium archive while Playwright fetches its small helpers.

    Playwright's own install downloads Chromium, extracts it on one thread and then fetches
    FFmpeg (and ``winldd`` on Windows) one after the other. Here the helpers download while
    the archive downloads and then unpacks on several threads. The archive is the
    installer's own download when it fetched it (``driver.installer_archive``). A failure
    only traces: the ordinary install that follows fetches whatever is still missing.
    """
    helpers = ("ffmpeg", "winldd") if os.name == "nt" else ("ffmpeg",)
    with concurrent.futures.ThreadPoolExecutor(1, "clio-chromium-helpers") as pool:
        fetching = pool.submit(_run_install, node, cli, root, helpers)
        archive: Path | None = None
        ours = False
        try:
            archive = driver.installer_archive(pin)
            if archive is None:
                archive = _fetch_pinned(pin, root)
                ours = True
            else:
                trace.event("BROWSER", "chromium taken from the installer's download")
            unpacked = _unpack_chromium(archive, root, str(pin["revision"]))
            trace.event("BROWSER", "chromium unpacked from the pinned archive=%s", unpacked)
        except (OSError, ValueError, zipfile.BadZipFile, http.client.HTTPException) as exc:
            trace.event("BROWSER", "installing the pinned chromium failed: %s", exc)
        finally:
            if archive is not None and ours:
                with contextlib.suppress(OSError):
                    archive.unlink()
        helped = fetching.result()
        if helped.returncode != 0:
            trace.event("BROWSER", "chromium helper fetch failed exit=%d", helped.returncode)


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
            pin = _chromium_pin()
            if pin is not None:
                _install_pinned(pin, node, cli, root)
            done = _run_install(node, cli, root)
            if done.returncode != 0:
                tail = (done.stderr or done.stdout or "").strip()[-1500:]
                trace.event(
                    "BROWSER", "chromium install failed rc=%s output=%s", done.returncode, tail
                )
                raise BrowserError(
                    REASON_INSTALL_FAILED,
                    install_failure(done.returncode, done.stdout or "", done.stderr or ""),
                )
        found = chromium_executable(root)
        if found is None:
            raise BrowserError(REASON_INSTALL_FAILED, f"Chromium is not where it went ({root})")
        trace.event("BROWSER", "chromium installed path=%s", found)
        return found


_TLS_CODES = frozenset(
    {
        "SELF_SIGNED_CERT_IN_CHAIN",
        "DEPTH_ZERO_SELF_SIGNED_CERT",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
        "UNABLE_TO_GET_ISSUER_CERT",
        "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        "CERT_HAS_EXPIRED",
        "ERR_TLS_CERT_ALTNAME_INVALID",
    }
)
_DNS_CODES = frozenset({"ENOTFOUND", "EAI_AGAIN"})
_NET_CODES = frozenset(
    {"ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "EPROTO"}
)
_CODE = re.compile(r"\bcode:\s*'([A-Z0-9_]+)'")
_SOURCE = re.compile(r"\bfrom (https?://[^/\s]+)")


def install_failure(returncode: int, stdout: str, stderr: str) -> str:
    """One readable sentence for a failed ``playwright install``: the cause, then what to do.

    Playwright retries the download several times and prints a Node.js error with its stack
    for each attempt; the stack frames and repeats say nothing a person can act on.
    """
    text = f"{stdout}\n{stderr}"
    source = _SOURCE.search(text)
    host = source.group(1).split("://", 1)[1] if source else "the Playwright download host"
    codes = list(dict.fromkeys(_CODE.findall(text)))
    errors = list(
        dict.fromkeys(
            line.strip().removesuffix(", caused by")
            for line in text.splitlines()
            if line.strip().startswith("Error:") and "Download failure" not in line
        )
    )
    said = "; ".join(errors[:3]) or f"exit {returncode}"
    if codes:
        said += f" ({', '.join(codes[:3])})"
    if _TLS_CODES.intersection(codes):
        cause = (
            f"the connection to {host} presented a certificate Node.js does not trust, "
            "usually a TLS-inspecting proxy or firewall. Point NODE_EXTRA_CA_CERTS at a PEM "
            "file holding that proxy's CA certificate, restart CLIO and try again"
        )
    elif _DNS_CODES.intersection(codes):
        cause = f"{host} could not be resolved; check the network connection or DNS"
    elif _NET_CODES.intersection(codes):
        cause = f"{host} could not be reached; check the network connection or HTTPS_PROXY"
    elif "ENOSPC" in codes or "no space left" in text.lower():
        cause = "the disk is full; free some space and try again"
    else:
        cause = f"the download from {host} did not complete"
    return f"Downloading Chromium failed: {cause}. Playwright reported: {said}."


def prepare_at_install() -> dict[str, Any]:
    """Fetch the driver's Node.js and the managed Chromium as an install step.

    The installer runs this beside its other package steps, so the first browser use finds
    both ready. A failure does not fail the install: it is reported typed, and the first
    browser use retries the same fetch.
    """
    from clio_agent.gact.browser import settings  # noqa: PLC0415

    try:
        if not playwright_version():
            return {"status": "skipped", "reason": REASON_NOT_INSTALLED}
        node = driver.ensure_node()
        if settings.backend() != "managed" or settings.channel() != "chromium":
            return {"status": "ready", "node": str(node or ""), "chromium": "not_used"}
        chromium = ensure_chromium()
    except BrowserError as exc:
        return {"status": "failed", "reason": exc.reason, "error": exc.message}
    except (ImportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "reason": REASON_INSTALL_FAILED, "error": str(exc)}
    return {"status": "ready", "node": str(node or ""), "chromium": str(chromium)}


_DOWNLOAD_LOCK = threading.Lock()
#: This process's Chromium download, while it runs.
_download: concurrent.futures.Future[Path] | None = None


def _download_in_background(task: concurrent.futures.Future[Path]) -> None:
    global _download
    found: Path | None = None
    error: BaseException | None = None
    try:
        found = ensure_chromium()
    except (BrowserError, ImportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        error = exc
    finally:
        # Forget the task before completing it, so an action that sees it fail starts afresh.
        with _DOWNLOAD_LOCK:
            if _download is task:
                _download = None
        if error is None and found is None:
            # Anything else still propagates (the thread's excepthook); waiters get it typed.
            error = BrowserError(
                REASON_INSTALL_FAILED, "the Chromium download stopped unexpectedly"
            )
        if error is not None:
            task.set_exception(error)
        elif found is not None:
            task.set_result(found)


def chromium_for_action(wait_s: float) -> Path:
    """The Chromium executable for a browser action, waiting at most ``wait_s`` for it.

    The first use downloads Chromium, which can take minutes. The download runs on its
    own thread and outlives the
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
    try:
        node = driver.node_path()
        details["driver"] = str(node) if node is not None else "fetched on first use"
    except BrowserError as exc:
        details["driver"] = exc.message
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
