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
    "create_login_flow",
    "drop_login_flow",
    "get_login_flow",
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
            with self._result.lock:
                self._result.status, self._result.reason = "failed", "cancelled"
        self._close_loopback()

    def status(self) -> tuple[FlowState, str]:
        with self._result.lock:
            return self._result.status, self._result.reason

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
        with self._result.lock:
            self._result.status = "complete"
        self._close_loopback()

    def _finish_failed(self, message: str) -> None:
        with self._result.lock:
            self._result.status, self._result.reason = "failed", message
        self._close_loopback()

    def _close_loopback(self) -> None:
        if self._loopback is not None:
            self._loopback.close()


# ---------------------------------------------------------------------------
# Pending-flow registry (mirrors ``argonne_auth``'s server-held, TTL'd,
# opaque-flow-id-keyed pending-authentication dict) -- the generic sign-in
# API's ``start``/``complete``/``status`` actions all address a flow by this id.
# ---------------------------------------------------------------------------

_FLOW_TTL_S = 15 * 60.0
_flows: dict[str, CodexLoginFlow] = {}
_flow_created_at: dict[str, float] = {}
_flows_lock = threading.Lock()


def create_login_flow() -> CodexLoginFlow:
    """Create and register a new login flow, sweeping expired ones first."""

    flow_id = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _flows_lock:
        expired = [fid for fid, created in _flow_created_at.items() if now - created > _FLOW_TTL_S]
        for fid in expired:
            stale = _flows.pop(fid, None)
            _flow_created_at.pop(fid, None)
            if stale is not None:
                stale.cancel()
        flow = CodexLoginFlow(flow_id)
        _flows[flow_id] = flow
        _flow_created_at[flow_id] = now
    return flow


def get_login_flow(flow_id: str) -> CodexLoginFlow | None:
    with _flows_lock:
        return _flows.get(flow_id.strip())


def drop_login_flow(flow_id: str) -> None:
    with _flows_lock:
        _flows.pop(flow_id, None)
        _flow_created_at.pop(flow_id, None)


def _reset_login_flows_for_tests() -> None:
    with _flows_lock:
        _flows.clear()
        _flow_created_at.clear()
