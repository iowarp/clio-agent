"""Session-owned work clients; per-forward views share pools, never usage history.

Only registered work forwards enter this owner. Judges and hook-created clients
retain operation lifetimes. Replacement retires an old client after its final
lease, so changing configuration cannot close another request's connection.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.lm.resources import (
    close_model_clients,
    model_resources,
    transfer_created_lm,
)
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.config import LMProviderConfig

_INSTALL_LOCK = threading.Lock()


@dataclass(eq=False)
class _Entry:
    config: LMProviderConfig
    lm: Any
    leases: int = 0
    retired: bool = False


class SessionModelClients:
    """Own work clients for an app, with independent session and agent scopes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._deleted: set[str] = set()
        self._closed = False

    def acquire(self, sid: str, scope: str, config: LMProviderConfig) -> tuple[_Entry, list[Any]]:
        """Lease the matching client; retire a replaced client without cancelling its users."""
        from clio_agent.config import create_lm  # noqa: PLC0415 - preserve factory seam

        key = (sid, scope)
        with self._lock:
            self._check_open(sid)
            entry = self._entries.get(key)
            if entry is not None and entry.config == config:
                entry.leases += 1
                return entry, []
        # Factory construction can probe a local provider. Never hold the app
        # registry lock while that runs; unrelated sessions remain independent.
        lm = create_lm(config)
        with self._lock:
            self._check_open(sid)
            entry = self._entries.get(key)
            if entry is not None and entry.config == config:
                entry.leases += 1
                # This unused racing construction remains operation-owned.
                return entry, []
            old = self._retire(entry)
            entry = _Entry(copy.deepcopy(config), lm, leases=1)
            transfer_created_lm(lm)
            self._entries[key] = entry
            return entry, old

    def _check_open(self, sid: str) -> None:
        if self._closed or sid in self._deleted:
            raise RuntimeError("The session model-client owner has closed")

    @staticmethod
    def _retire(entry: _Entry | None) -> list[Any]:
        if entry is None:
            return []
        entry.retired = True
        return [entry.lm] if entry.leases == 0 else []

    def release(self, entry: _Entry) -> list[Any]:
        """Release one use; return a retired client only after its final user finishes."""
        with self._lock:
            entry.leases -= 1
            return [entry.lm] if entry.retired and entry.leases == 0 else []

    def close(self, sid: str | None = None) -> list[Any]:
        """Retire one deleted session or the whole app; active users close on release."""
        with self._lock:
            if sid is None:
                self._closed = True
            else:
                self._deleted.add(sid)
            keys = [key for key in self._entries if sid is None or key[0] == sid]
            return [lm for key in keys for lm in self._retire(self._entries.pop(key))]


def _owner(app: Any) -> SessionModelClients:
    with _INSTALL_LOCK:
        owner = getattr(app.state, "session_model_clients", None)
        if owner is None:
            owner = SessionModelClients()
            app.state.session_model_clients = owner
        return owner


@contextmanager
def session_model(config: LMProviderConfig, scope: str, sid: str) -> Iterator[Any]:
    """Borrow a session's work pool with isolated history and current model hooks.

    Credentials are resolved by the caller on every forward. A changed credential,
    endpoint, model or generation configuration replaces the matching scope's
    client, closing its old pools after their last user. No client is shared with
    another session. Outside an app session, clients retain operation cleanup.
    """
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.lm.hooked_lm import create_hooked_lm, wrap_lm_with_hooks  # noqa: PLC0415

    with model_resources():
        app = context.active_app()
        if app is None or not sid:
            yield create_hooked_lm(config)
            return
        if app.state.sessions.get(sid) is None:
            raise RuntimeError("The session no longer exists")
        owner = _owner(app)
        entry, old = owner.acquire(sid, scope, config)
        primary: BaseException | None = None
        try:
            close_model_clients(old)
            # Public DSPy copy: shared owned engines; fresh history/callback list/
            # request kwargs. This is a view, not another provider client or pool.
            view = entry.lm.copy()
            stream_audit(
                "lm.work_client.leased", session_id=sid, scope=scope, client_id=id(entry.lm)
            )
            yield wrap_lm_with_hooks(view)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            close_model_clients(owner.release(entry), primary)
            stream_audit(
                "lm.work_client.released", session_id=sid, scope=scope, client_id=id(entry.lm)
            )


def close_session_model_clients(app: Any, sid: str | None = None) -> None:
    """Release a deleted session's pools, or all work pools after app turn drain."""
    # Install a closed owner even before the first client. A worker abandoned
    # during turn drain must not recreate a pool after shutdown has completed.
    close_model_clients(_owner(app).close(sid))
