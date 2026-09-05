"""Workspace-volume-contained scratch lifecycle for the CLIO test suite."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path

_RUNTIME_PARENT_SUFFIX = ".pytest-runtime"
_OWNER_FILE = ".clio-test-run.json"
_RUN_NAME = re.compile(r"run-(?P<pid>[0-9]+)-[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True)
class TestRuntime:
    """One pytest process's owned scratch layout."""

    parent: Path
    root: Path
    temp_dir: Path
    pytest_dir: Path
    cte_dir: Path
    cache_dir: Path
    state_dir: Path


def resolve_test_runtime_parent(
    checkout: Path,
    environ: MutableMapping[str, str],
) -> Path:
    """Return the explicit scratch parent, defaulting beside the checkout."""
    configured = environ.get("CLIO_TEST_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    resolved_checkout = checkout.resolve()
    return (
        resolved_checkout.parent / f".{resolved_checkout.name}{_RUNTIME_PARENT_SUFFIX}"
    ).resolve()


def create_test_runtime(
    checkout: Path,
    environ: MutableMapping[str, str],
    *,
    pid: int | None = None,
    nonce: str | None = None,
) -> TestRuntime:
    """Create one run-owned layout and redirect every conventional temp variable to it."""
    owner_pid = os.getpid() if pid is None else pid
    run_nonce = secrets.token_hex(6) if nonce is None else nonce
    if owner_pid < 1 or re.fullmatch(r"[A-Za-z0-9_-]+", run_nonce) is None:
        raise ValueError("invalid test runtime identity")

    parent = resolve_test_runtime_parent(checkout, environ)
    root = parent / f"run-{owner_pid}-{run_nonce}"
    root.mkdir(parents=True, exist_ok=False)
    temp_dir = root / "tmp"
    pytest_dir = root / "pytest"
    cte_dir = root / "cte"
    cache_dir = root / "cache"
    state_dir = root / "state"
    for directory in (temp_dir, pytest_dir, cte_dir, cache_dir, state_dir):
        directory.mkdir()
    root.joinpath(_OWNER_FILE).write_text(
        json.dumps({"pid": owner_pid, "checkout": str(checkout.resolve())}),
        encoding="utf-8",
    )

    for name in ("TEMP", "TMP", "TMPDIR"):
        environ[name] = str(temp_dir)
    contained_paths = {
        "UV_CACHE_DIR": cache_dir / "uv",
        "PIP_CACHE_DIR": cache_dir / "pip",
        "PYTHONPYCACHEPREFIX": cache_dir / "pycache",
        "XDG_CACHE_HOME": cache_dir / "xdg",
        "XDG_STATE_HOME": state_dir / "xdg",
    }
    for name, path in contained_paths.items():
        path.mkdir(parents=True)
        environ[name] = str(path)
    environ["CLIO_TEST_RUNTIME_DIR"] = str(root)
    return TestRuntime(
        parent=parent,
        root=root,
        temp_dir=temp_dir,
        pytest_dir=pytest_dir,
        cte_dir=cte_dir,
        cache_dir=cache_dir,
        state_dir=state_dir,
    )


def _pid_is_live(pid: int) -> bool:
    try:
        import psutil  # noqa: PLC0415

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True


def stale_test_runtimes(
    parent: Path,
    *,
    pid_is_live: Callable[[int], bool] = _pid_is_live,
) -> list[Path]:
    """Return dead, marker-authenticated run roots without touching active or unrelated data."""
    if not parent.is_dir():
        return []
    stale: list[Path] = []
    for candidate in sorted(parent.iterdir()):
        match = _RUN_NAME.fullmatch(candidate.name)
        if match is None or not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            owner = json.loads(candidate.joinpath(_OWNER_FILE).read_text(encoding="utf-8"))
            pid = int(owner["pid"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if pid != int(match.group("pid")):
            continue
        if not pid_is_live(pid):
            stale.append(candidate)
    return stale


def _clear_readonly_and_retry(
    operation: Callable[[str], object],
    path: str,
    _exc_info: object,
) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    operation(path)


def _removal_path(path: Path) -> str:
    """Return a Windows extended-length path so deep plugin caches can be removed."""

    resolved = str(path.resolve())
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        return f"\\\\?\\{resolved}"
    return resolved


def cleanup_test_runtime(
    root: Path,
    parent: Path,
    *,
    attempts: int = 8,
    retry_delay_seconds: float = 0.1,
) -> None:
    """Remove one identity-stable owned run, including readonly Windows fixture files."""
    if attempts < 1 or retry_delay_seconds < 0:
        raise ValueError("test runtime cleanup retry bounds are invalid")
    expected_parent = parent.resolve()
    if root.parent.resolve() != expected_parent or _RUN_NAME.fullmatch(root.name) is None:
        raise RuntimeError(f"refusing to remove an unexpected test runtime: {root}")
    try:
        initial = root.lstat()
    except FileNotFoundError:
        return
    file_attributes = getattr(initial, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISDIR(initial.st_mode)
        or stat.S_ISLNK(initial.st_mode)
        or (reparse_attribute and file_attributes & reparse_attribute)
    ):
        raise RuntimeError(f"refusing to remove a linked test runtime: {root}")
    identity = (initial.st_dev, initial.st_ino)
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            current = root.lstat()
            if (current.st_dev, current.st_ino) != identity:
                raise RuntimeError(f"test runtime identity changed before cleanup: {root}")
            shutil.rmtree(_removal_path(root), onexc=_clear_readonly_and_retry)
            if root.exists() or root.is_symlink():
                raise OSError(f"test runtime remained after cleanup: {root}")
            try:
                expected_parent.rmdir()
            except OSError:
                pass
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(retry_delay_seconds)
    raise RuntimeError(f"could not remove test runtime: {root}") from last_error
