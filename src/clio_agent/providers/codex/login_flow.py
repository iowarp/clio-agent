"""The Codex credential record and the login-flow orchestrator.

Split out of :mod:`clio_agent.providers.codex.oauth` (which owns the
stateless protocol mechanics: PKCE, the loopback listener, paste parsing,
device login, code exchange/refresh, JWT decode) to keep that module under
the #775 file-size ratchet. :class:`CodexLoginFlow` is the stateful
orchestrator a route handler drives across the generic
start/complete/status/logout auth API (see
:mod:`clio_agent.gact.routes.provider_auth`); the module-level registry at
the bottom mirrors ``argonne_auth``'s server-held, TTL'd, opaque-flow-id-keyed
pending-authentication dict.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.oauth import (
    DeviceLogin,
    LoopbackBindError,
    LoopbackListener,
    OAuthError,
    build_authorize_url,
    decode_account_id,
    exchange_code,
    generate_pkce,
    parse_paste,
    poll_device_login,
    start_device_login,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CodexCredential",
    "CodexLoginFlow",
    "FlowState",
    "LoginMethods",
    "drop_login_flow",
    "get_login_flow",
    "start_login",
]

#: How long :class:`CodexLoginFlow` waits on the loopback callback before
#: giving up (a paste can still complete the flow after this).
_BROWSER_WAIT_TIMEOUT_S = 15 * 60.0

FlowState = Literal["pending", "complete", "failed"]


@dataclass(frozen=True)
class CodexCredential:
    """One stored Codex OAuth credential (A.3's ``credential`` shape)."""

    access_token: str
    refresh_token: str
    expires_at_ms: int
    account_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "oauth",
            "provider": c.PROVIDER_ID,
            "access": self.access_token,
            "refresh": self.refresh_token,
            "expires_at_ms": self.expires_at_ms,
            "account_id": self.account_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CodexCredential":
        return cls(
            access_token=str(raw.get("access") or ""),
            refresh_token=str(raw.get("refresh") or ""),
            expires_at_ms=int(raw.get("expires_at_ms") or 0),
            account_id=str(raw.get("account_id") or ""),
        )


@dataclass
class LoginMethods:
    """What :meth:`CodexLoginFlow.start_browser`/``start_device`` hand the client.

    Matches the generic auth API's ``start`` response shape: ``{flow_id,
    browser?: {authorization_url, loopback}, device?: {user_code,
    verification_url, interval}}``.
    """

    flow_id: str
    browser: dict[str, Any] | None = None
    device: dict[str, Any] | None = None
    instructions: str = ""


@dataclass
class _FlowResult:
    status: FlowState = "pending"
    reason: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    claimed: bool = False
    #: Set exactly when ``status`` leaves ``"pending"`` -- the flow's own
    #: settlement signal, so a waiter observes the terminal state itself
    #: rather than an upstream event (e.g. the loopback callback) that the
    #: background resolver thread has not finished acting on yet.
    settled: threading.Event = field(default_factory=threading.Event)


class CodexLoginFlow:
    """One in-progress Codex sign-in attempt.

    Owns the PKCE verifier and CSRF state for its lifetime (one flow, one
    attempt). The browser method races the loopback listener against a pasted
    redirect (:meth:`submit_paste`) — whichever resolves first claims the flow
    (:meth:`_claim`) and the other side is torn down, never double-processed.
    The device method polls in a background thread. Every path funnels
    through :meth:`status`/:meth:`credential`, which a route handler polls.
    """

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id
        self._pkce = generate_pkce()
        self._redirect_uri = c.REDIRECT_URI
        self._result = _FlowResult()
        self._credential: CodexCredential | None = None
        self._loopback: LoopbackListener | None = None
        #: The `start_browser`/`start_device` result, cached so a second
        #: `start` call for the SAME still-pending flow (see `start_login`)
        #: can return it verbatim instead of starting a second listener/poll.
        self._methods: LoginMethods | None = None

    def cached_methods(self) -> LoginMethods | None:
        return self._methods

    def set_cached_methods(self, methods: LoginMethods) -> None:
        self._methods = methods

    def _claim(self) -> bool:
        with self._result.lock:
            if self._result.claimed:
                return False
            self._result.claimed = True
            return True

    def start_browser(self) -> LoginMethods:
        """Start the loopback listener (if the port is free) and return the browser URL."""

        browser: dict[str, Any] = {
            "authorization_url": build_authorize_url(self._pkce),
            "loopback": False,
        }
        listener = LoopbackListener(self._pkce.state)
        try:
            listener.start()
        except LoopbackBindError as exc:
            logger.info("codex oauth: loopback unavailable, falling back to paste-only: %s", exc)
            browser["loopback_unavailable_reason"] = "port_in_use"
        else:
            self._loopback = listener
            browser["loopback"] = True
            threading.Thread(
                target=self._await_loopback, daemon=True, name="codex-oauth-wait"
            ).start()
        return LoginMethods(
            flow_id=self.flow_id,
            browser=browser,
            instructions=(
                "Open the link to sign in with your Codex account, or paste the "
                "redirect URL here once you land on the localhost page."
            ),
        )

    def start_device(self) -> LoginMethods:
        """Start the device-code flow and begin polling for completion in the background."""

        login = start_device_login()
        self._redirect_uri = c.DEVICE_REDIRECT_URI
        threading.Thread(
            target=self._run_device_poll, args=(login,), daemon=True, name="codex-oauth-device"
        ).start()
        return LoginMethods(
            flow_id=self.flow_id,
            device={
                "user_code": login.user_code,
                "verification_url": login.verification_url,
                "interval": login.interval_s,
            },
            instructions=f"Enter code {login.user_code} at {login.verification_url}.",
        )

    def submit_paste(self, raw: str) -> None:
        """Feed a pasted redirect into the race. A no-op once the flow is resolved."""

        try:
            code, state = parse_paste(raw, expected_state=self._pkce.state)
        except OAuthError as exc:
            if self._claim():
                self._finish_failed(str(exc))
            return
        del state  # parse_paste already enforced the state match when present
        if not self._claim():
            return
        self._exchange_and_finish(code=code, code_verifier=self._pkce.verifier)

    def cancel(self) -> None:
        """Abandon the flow: close the loopback listener and mark it failed."""

        if self._claim():
            self._settle("failed", "cancelled")
        self._close_loopback()

    def status(self) -> tuple[FlowState, str]:
        with self._result.lock:
            return self._result.status, self._result.reason

    def wait_settled(self, timeout: float) -> tuple[FlowState, str]:
        """Block until the flow leaves ``"pending"`` (or ``timeout`` elapses).

        The browser/device paths resolve on background threads, so
        :meth:`status` read right after the triggering event (a loopback
        callback, a device-poll answer) can still be ``"pending"``. This waits
        on the flow's own settlement signal instead of that upstream event.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            The ``(status, reason)`` pair at return time -- still
            ``("pending", "")`` only if ``timeout`` elapsed first.
        """

        self._result.settled.wait(timeout=timeout)
        return self.status()

    def credential(self) -> CodexCredential | None:
        return self._credential

    # -- internals -------------------------------------------------------

    def _await_loopback(self) -> None:
        assert self._loopback is not None  # noqa: S101 - only called after a successful start()
        try:
            code, _state = self._loopback.wait_for_code(_BROWSER_WAIT_TIMEOUT_S)
        except OAuthError as exc:
            if self._claim():
                self._finish_failed(str(exc))
            return
        if not self._claim():
            return
        self._exchange_and_finish(code=code, code_verifier=self._pkce.verifier)

    def _run_device_poll(self, login: DeviceLogin) -> None:
        try:
            result = poll_device_login(login)
        except OAuthError as exc:
            if self._claim():
                self._finish_failed(str(exc))
            return
        if not self._claim():
            return
        self._exchange_and_finish(
            code=result.authorization_code, code_verifier=result.code_verifier
        )

    def _exchange_and_finish(self, *, code: str, code_verifier: str) -> None:
        try:
            tokens = exchange_code(
                code=code, code_verifier=code_verifier, redirect_uri=self._redirect_uri
            )
            account_id = decode_account_id(tokens.access_token)
        except OAuthError as exc:
            self._finish_failed(str(exc))
            return
        self._credential = CodexCredential(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_at_ms=int(time.time() * 1000) + tokens.expires_in * 1000,
            account_id=account_id,
        )
        self._settle("complete", "")
        self._close_loopback()

    def _finish_failed(self, message: str) -> None:
        self._settle("failed", message)
        self._close_loopback()

    def _settle(self, status: FlowState, reason: str) -> None:
        with self._result.lock:
            self._result.status, self._result.reason = status, reason
        self._result.settled.set()

    def _close_loopback(self) -> None:
        if self._loopback is not None:
            self._loopback.close()


# ---------------------------------------------------------------------------
# ONE active flow, ever (#1 of the owner's live-tested OAuth defects). Codex
# sign-in is machine-wide (one credential per machine, `credentials.py`), and
# the loopback listener can only ever bind ONE port -- so there is at most
# ONE pending flow at a time, not a dict of them. `start_login` is the single
# entry point every `start` action goes through: a still-pending flow for the
# SAME method is returned verbatim (same flow_id, same PKCE state, no second
# listener); anything else (an explicit retry, a method change, expiry, or
# a resolved flow) cancels the old one -- closing its listener -- before a
# fresh one starts. Running two listeners/states at once is exactly how a
# retried sign-in produced "state mismatch": the second flow's browser URL
# carried a state the (only one that could bind the port) first flow's
# listener never expected.
# ---------------------------------------------------------------------------

_FLOW_TTL_S = 15 * 60.0
_current: CodexLoginFlow | None = None
_current_created_at: float = 0.0
_current_method: str = ""
_flows_lock = threading.Lock()


def _expired(created_at: float) -> bool:
    return time.monotonic() - created_at > _FLOW_TTL_S


def start_login(*, method: str = "browser", force: bool = False) -> LoginMethods:
    """Start (or idempotently resume) THE ONE Codex login flow.

    Args:
        method: ``"browser"`` or ``"device"``. A change from the pending
            flow's own method is treated the same as ``force``.
        force: An explicit user "Sign in" click. Always cancels whatever was
            pending and starts clean, so a retry never contends with a stale
            attempt for the loopback port.

    Never runs two listeners/states concurrently: that invariant is the
    entire point of this function, not an incidental property of it.
    """
    global _current, _current_created_at, _current_method  # noqa: PLW0603
    with _flows_lock:
        stale = _current
        can_reuse = False
        if stale is not None and not force and method == _current_method:
            state, _reason = stale.status()
            can_reuse = (
                state == "pending"
                and not _expired(_current_created_at)
                and stale.cached_methods() is not None
            )
        if can_reuse:
            cached = stale.cached_methods() if stale is not None else None
            if cached is not None:
                return cached
        if stale is not None:
            logger.info(
                "codex oauth: replacing the pending flow before starting a new one "
                "reason=%s",
                "forced" if force else "method_changed_or_resolved",
            )
            stale.cancel()
        flow_id = secrets.token_urlsafe(32)
        flow = CodexLoginFlow(flow_id)
        _current = flow
        _current_created_at = time.monotonic()
        _current_method = method
    # The side-effecting part (binding the loopback socket / calling the
    # device-code endpoint) happens OUTSIDE the lock -- it must never block a
    # concurrent status/complete lookup, which only ever reads _current.
    methods = flow.start_device() if method == "device" else flow.start_browser()
    flow.set_cached_methods(methods)
    return methods


def _set_current_flow_for_tests(flow: CodexLoginFlow) -> None:
    """Register a manually-constructed flow as THE current one (tests only).

    A test that wants to simulate a flow already at some state (e.g.
    "complete", to exercise `status`'s credential-persist path) constructs a
    plain :class:`CodexLoginFlow`, mutates its private state directly, and
    registers it here so :func:`get_login_flow` finds it -- without going
    through :func:`start_login`'s real side effects (binding the loopback
    socket, calling the device-code endpoint).
    """

    global _current, _current_created_at, _current_method  # noqa: PLW0603
    with _flows_lock:
        _current = flow
        _current_created_at = time.monotonic()
        _current_method = "browser"


def get_login_flow(flow_id: str) -> CodexLoginFlow | None:
    with _flows_lock:
        if _current is not None and _current.flow_id == flow_id.strip():
            return _current
        return None


def drop_login_flow(flow_id: str) -> None:
    global _current  # noqa: PLW0603
    with _flows_lock:
        if _current is not None and _current.flow_id == flow_id.strip():
            _current = None


def _reset_login_flows_for_tests() -> None:
    global _current, _current_created_at, _current_method  # noqa: PLW0603
    with _flows_lock:
        _current = None
        _current_created_at = 0.0
        _current_method = ""
