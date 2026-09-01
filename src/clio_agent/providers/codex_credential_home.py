"""Private ``CODEX_HOME`` lifecycle for the official Codex SDK transport.

Empty config overrides do not erase configuration contributed by personal Codex
plugins, so the SDK runtime is handed a private home seeded with only
``auth.json``. Each such home holds a 0600 copy of the user's real credentials,
which makes its lifetime a security property rather than housekeeping: the number
of live copies is capped, every home records its owning pid, and a home whose
owner is gone is reaped.

Split out of :mod:`clio_agent.providers.codex_stream` so the credential-home
lifecycle has one owner (and the transport module stays under the size ratchet).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_LIVE_CODEX_HOMES = 4

_CODEX_HOME_LOCK = threading.Lock()
_LIVE_CODEX_HOMES: set[Path] = set()
_CODEX_HOME_OWNER = ".clio-owner-pid"
#: The reaper's glob. A directory carrying this prefix is claimable by any CLIO
#: process's reaper, so it must never exist without its owner marker.
_CODEX_HOME_PREFIX = "clio-codex-sdk-"
#: Staging prefix, deliberately NOT matched by :data:`_CODEX_HOME_PREFIX`'s glob.
_CODEX_HOME_STAGING_PREFIX = "clio-codex-stage-"


def _capacity_error() -> Exception:
    """Return the transport's typed capacity failure.

    Imported late: :mod:`clio_agent.providers.codex_stream` imports this module at
    module scope, so the error type is only reachable once that import completed.
    """

    from clio_agent.providers.codex_stream import CodexSDKError  # noqa: PLC0415

    return CodexSDKError(
        "Codex SDK private credential-home capacity reached reason=credential_home_capacity"
    )


def _process_is_alive(pid: int) -> bool:
    """Return whether ``pid`` is alive without terminating or signalling it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reap_orphaned_codex_homes(temp_root: Path | None = None) -> list[Path]:
    """Remove private SDK homes whose owning process no longer exists."""
    root = temp_root or Path(tempfile.gettempdir())
    reaped: list[Path] = []
    for home in root.glob(f"{_CODEX_HOME_PREFIX}*"):
        if home in _LIVE_CODEX_HOMES or not home.is_dir():
            continue
        try:
            pid = int((home / _CODEX_HOME_OWNER).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = -1
        if _process_is_alive(pid):
            continue
        try:
            shutil.rmtree(home)
            reaped.append(home)
        except OSError as exc:
            logger.warning(
                "Codex SDK credential reaper could not remove home "
                "reason=credential_home_reap_failed path=%s error=%r",
                home,
                exc,
            )
    if reaped:
        logger.info(
            "Codex SDK credential reaper removed orphaned homes "
            "reason=credential_homes_reaped count=%d",
            len(reaped),
        )
    return reaped


def _mint_owned_codex_home() -> Path:
    """Create a private home that already carries its owner pid when it becomes visible.

    ``_CODEX_HOME_LOCK`` is a :class:`threading.Lock`, so it cannot serialise a second
    CLIO process's reaper. The only cross-process guard is ordering: the directory is
    staged under a prefix the reaper's glob does not match, stamped with the owning pid,
    and only then renamed into place. A reaper therefore never observes a claimable home
    without an owner marker and can never delete a home this process is still filling.

    Returns:
        The path of the new, owner-stamped private home.

    Raises:
        OSError: When the home cannot be created, stamped, or renamed.
    """

    staged = Path(tempfile.mkdtemp(prefix=_CODEX_HOME_STAGING_PREFIX))
    try:
        (staged / _CODEX_HOME_OWNER).write_text(str(os.getpid()), encoding="ascii")
        home = staged.with_name(_CODEX_HOME_PREFIX + staged.name[len(_CODEX_HOME_STAGING_PREFIX) :])
        staged.rename(home)
    except OSError:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return home


class IsolatedCodexHome:
    """Give the SDK authentication without personal Codex capabilities.

    Empty config overrides do not erase configuration contributed by personal
    plugins. The SDK runtime therefore receives a private ``CODEX_HOME`` seeded
    with only ``auth.json``. If the runtime rotates credentials, the refreshed
    file is copied back only when the source has not changed concurrently.
    """

    def __init__(self) -> None:
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        source_home = Path(configured_home) if configured_home else Path.home() / ".codex"
        self._source_auth = source_home / "auth.json"
        self._temporary_home: Path | None = None
        self._seed_digest = ""

    def start(self) -> dict[str, str]:
        """Create the private home and return environment overrides for the SDK."""
        if self._temporary_home is not None:
            return self._environment(self._temporary_home)

        with _CODEX_HOME_LOCK:
            _reap_orphaned_codex_homes()
            if len(_LIVE_CODEX_HOMES) >= MAX_LIVE_CODEX_HOMES:
                raise _capacity_error()
            home = _mint_owned_codex_home()
            _LIVE_CODEX_HOMES.add(home)
        auth_target = home / "auth.json"
        try:
            if self._source_auth.is_file():
                auth_bytes = self._source_auth.read_bytes()
                auth_target.write_bytes(auth_bytes)
                with contextlib.suppress(OSError):
                    auth_target.chmod(0o600)
                self._seed_digest = hashlib.sha256(auth_bytes).hexdigest()
            (home / "sqlite").mkdir(exist_ok=True)
        except Exception:
            shutil.rmtree(home, ignore_errors=True)
            with _CODEX_HOME_LOCK:
                _LIVE_CODEX_HOMES.discard(home)
            raise
        self._temporary_home = home
        return self._environment(home)

    @staticmethod
    def _environment(home: Path) -> dict[str, str]:
        return {
            "CODEX_HOME": str(home),
            "CODEX_SQLITE_HOME": str(home / "sqlite"),
        }

    def close(self) -> None:
        """Persist an uncontended auth rotation, then remove the private home."""
        home, self._temporary_home = self._temporary_home, None
        if home is None:
            return
        try:
            isolated_auth = home / "auth.json"
            if isolated_auth.is_file() and self._seed_digest:
                updated = isolated_auth.read_bytes()
                updated_digest = hashlib.sha256(updated).hexdigest()
                if updated_digest != self._seed_digest:
                    current = self._source_auth.read_bytes()
                    current_digest = hashlib.sha256(current).hexdigest()
                    if current_digest == self._seed_digest:
                        staged = self._source_auth.with_name(
                            f".{self._source_auth.name}.clio-{uuid.uuid4().hex}.tmp"
                        )
                        try:
                            staged.write_bytes(updated)
                            with contextlib.suppress(OSError):
                                staged.chmod(0o600)
                            os.replace(staged, self._source_auth)
                        finally:
                            with contextlib.suppress(OSError):
                                staged.unlink()
                    else:
                        logger.warning(
                            "Codex SDK refreshed auth was not copied back because the "
                            "source changed concurrently reason=auth_source_changed"
                        )
        except Exception:  # noqa: BLE001 - teardown reports but still removes secrets
            logger.warning(
                "Codex SDK isolated auth reconciliation failed reason=auth_reconcile_failed",
                exc_info=True,
            )
        finally:
            self._remove_private_home(home)
            with _CODEX_HOME_LOCK:
                _LIVE_CODEX_HOMES.discard(home)

    @staticmethod
    def _remove_private_home(home: Path) -> None:
        """Remove the SDK home after Windows releases its SQLite handles."""
        last_error: OSError | None = None
        for _attempt in range(20):
            try:
                shutil.rmtree(home)
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.1)
        logger.error(
            "Codex SDK private home cleanup failed reason=private_home_cleanup_failed "
            "path=%s error=%r",
            home,
            last_error,
        )


__all__ = [
    "MAX_LIVE_CODEX_HOMES",
    "IsolatedCodexHome",
    "_reap_orphaned_codex_homes",
]
