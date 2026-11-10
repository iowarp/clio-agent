"""Invalid MCP server environments are rebuilt, never kept (owner module).

clio-kit builds each MCP server's Python environment under
``<kit cache>/mcp-environments/<server>-<hash>`` and CLIO builds a ``uv run --project``
server's under ``mcp-project-envs/<name>-<hash>``. A build or removal cut short (a
force-killed server, a concurrent server evicting the directory, a delete that a held
file stopped half way) can leave that directory without its interpreter. uv refuses such
a directory forever ("not a valid Python environment (no Python executable was
found)"), so the server's tools stayed broken until someone deleted it by hand.

:func:`environment_problem` names what makes a directory invalid. Before a server is
prepared or started, :func:`repair_invalid_environments` moves every invalid environment
that no live launcher holds aside and removes it (``mcp_environment_repaired``), so the
build that follows starts from nothing. Every move happens under the environment's
cross-process lock (:func:`environment_lock`), the same lock the atomic build in
:data:`clio_agent.tools.mcp_prepare._KIT_BUILD_SHIM` takes to rename a finished build into
place, so a repair never removes an environment another process has just completed.
A removal is a rename first, so an interrupted removal leaves a hidden leftover (cleared
on a later sweep), never a half-deleted environment under the live name.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clio_agent.runtime import trace

logger = logging.getLogger(__name__)

MCP_ENVIRONMENT_REPAIRED = "mcp_environment_repaired"
MCP_ENVIRONMENT_INVALID_IN_USE = "mcp_environment_invalid_in_use"
MCP_ENVIRONMENT_REPAIR_FAILED = "mcp_environment_repair_failed"

#: What makes an environment directory invalid (closed set).
NO_PYVENV_CFG = "no_pyvenv_cfg"
NO_INTERPRETER = "no_interpreter"
CORRUPT_INTERPRETER = "corrupt_interpreter"

_ENVIRONMENTS = "mcp-environments"
#: Beside clio-kit's own trees, so its cache upkeep never mistakes a lock for a server.
LOCKS_DIRNAME = "clio-environment-locks"
#: Hidden siblings: a staged build (``.<name>.build-<pid>``) or a removal in progress
#: (``.<name>.removed-<pid>-<ns>``). Neither matches clio-kit's ``<server>-<hash>``.
_STAGING_MARK = ".build-"
_REMOVED_MARK = ".removed-"
_LOCK_POLL_S = 0.05


def interpreter_path(environment: Path) -> Path:
    """The interpreter of the virtual environment at ``environment``."""

    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def environment_problem(environment: Path) -> str | None:
    """Why ``environment`` is not a usable virtual environment, or ``None``.

    An absent or empty directory is not a problem: uv builds into it. Anything else
    must be a complete environment: ``pyvenv.cfg`` (uv writes it as the environment's
    completion marker) and an interpreter that is a real executable.
    """

    try:
        if not environment.is_dir() or not any(environment.iterdir()):
            return None
    except OSError:
        return None
    if not (environment / "pyvenv.cfg").is_file():
        return NO_PYVENV_CFG
    interpreter = interpreter_path(environment)
    if not interpreter.exists():  # follows the POSIX symlink to the base interpreter
        return NO_INTERPRETER
    if os.name == "nt":
        try:
            with interpreter.open("rb") as handle:
                if handle.read(2) != b"MZ":
                    return CORRUPT_INTERPRETER
        except OSError:
            return CORRUPT_INTERPRETER
    return None


def _try_lock(fd: int) -> bool:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def environment_lock(lock_root: Path, name: str, *, timeout: float) -> Iterator[bool]:
    """Hold environment ``name``'s cross-process lock under ``lock_root``.

    Yields whether the lock was acquired within ``timeout`` seconds. It is the same
    OS lock (one byte locked at offset 0 of ``<lock_root>/<name>.lock``) that the build
    shim takes, so the two exclude each other across processes and interpreters.
    """

    lock_root.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_root / f"{name}.lock", os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + timeout
    acquired = _try_lock(fd)
    while not acquired and time.monotonic() < deadline:
        time.sleep(_LOCK_POLL_S)
        acquired = _try_lock(fd)
    if not acquired:
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        _unlock(fd)


def remove_environment(environment: Path) -> None:
    """Remove ``environment`` by renaming it aside first, then deleting the leftover.

    Raises:
        OSError: The rename failed (a file in it is held open): it is left in place.
    """

    aside = environment.with_name(
        f".{environment.name}{_REMOVED_MARK}{os.getpid()}-{time.monotonic_ns()}"
    )
    os.replace(environment, aside)
    shutil.rmtree(aside, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return True
    return psutil.pid_exists(pid)


def _clear_leftovers(root: Path) -> None:
    """Remove hidden removals and staged builds whose process is gone."""

    for child in root.iterdir():
        name = child.name
        if not name.startswith(".") or not child.is_dir():
            continue
        if _REMOVED_MARK in name:
            pid_text = name.rsplit(_REMOVED_MARK, 1)[1].split("-", 1)[0]
        elif _STAGING_MARK in name:
            pid_text = name.rsplit(_STAGING_MARK, 1)[1]
        else:
            continue
        if pid_text.isdigit() and not _pid_alive(int(pid_text)):
            shutil.rmtree(child, ignore_errors=True)


def repair_environment(
    environment: Path,
    lock_root: Path,
    *,
    in_use: bool = False,
    timeout: float = 30.0,
    lock_held: bool = False,
) -> str | None:
    """Remove ``environment`` if it is invalid; return the problem it had, else ``None``.

    The removal happens under the environment's lock (already held by the caller when
    ``lock_held``). An environment a live launcher holds (``in_use``) is left and logged
    ``mcp_environment_invalid_in_use``; one the lock or a held file keeps from being
    removed is logged ``mcp_environment_repair_failed`` (its build then fails with the
    typed ``mcp_prepare_provision_environment_invalid``).
    """

    problem = environment_problem(environment)
    if problem is None:
        return None
    if in_use:
        logger.warning(
            "reason=%s environment=%s problem=%s (a live launcher holds it; it is "
            "rebuilt once that launcher exits)",
            MCP_ENVIRONMENT_INVALID_IN_USE,
            environment,
            problem,
        )
        return None
    if lock_held:
        return _remove_invalid(environment)
    with environment_lock(lock_root, environment.name, timeout=timeout) as locked:
        if not locked:
            logger.warning(
                "reason=%s environment=%s problem=%s detail=lock busy for %.0fs",
                MCP_ENVIRONMENT_REPAIR_FAILED,
                environment,
                problem,
                timeout,
            )
            return None
        return _remove_invalid(environment)


def _remove_invalid(environment: Path) -> str | None:
    """Remove ``environment`` if it is (still) invalid; the caller holds its lock."""

    problem = environment_problem(environment)  # another process may have rebuilt it
    if problem is None:
        return None
    try:
        remove_environment(environment)
    except OSError as exc:
        logger.warning(
            "reason=%s environment=%s problem=%s detail=%r",
            MCP_ENVIRONMENT_REPAIR_FAILED,
            environment,
            problem,
            exc,
        )
        return None
    logger.warning(
        "reason=%s environment=%s problem=%s action=removed; rebuilt on this launch",
        MCP_ENVIRONMENT_REPAIRED,
        environment,
        problem,
    )
    trace.event(
        "TOOLS",
        "%s environment=%s problem=%s",
        MCP_ENVIRONMENT_REPAIRED,
        environment,
        problem,
    )
    return problem


def repair_invalid_environments(cache_root: Path) -> list[str]:
    """Remove the invalid clio-kit environments under ``cache_root`` (every call).

    Returns:
        The environment names removed.
    """

    root = cache_root / _ENVIRONMENTS
    if not root.is_dir():
        return []
    from clio_kit.environment_locks import environment_in_use  # noqa: PLC0415 - pinned dep

    lock_root = cache_root / LOCKS_DIRNAME
    repaired: list[str] = []
    try:
        _clear_leftovers(root)
        environments = sorted(root.iterdir())
    except OSError:
        return []
    for environment in environments:
        if environment.name.startswith(".") or environment.is_symlink():
            continue
        if environment_problem(environment) is None:
            continue
        in_use = environment_in_use(cache_root, environment.name)
        if repair_environment(environment, lock_root, in_use=in_use):
            repaired.append(environment.name)
    return repaired
