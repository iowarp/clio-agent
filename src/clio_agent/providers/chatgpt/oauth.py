"""ChatGPT subscription login: PKCE, the three login methods, and code exchange.

Implements brief Part A.3's protocol mechanics exactly: browser + loopback
(default), paste the redirect URL (fallback / always raced against the
loopback), and device code (headless: clio-relay HPC login nodes, any server
with no local browser). All three end in the same code exchange
(:func:`exchange_code`). The stateful orchestrator that drives these three
methods (:class:`~clio_agent.providers.chatgpt.login_flow.ChatGptLoginFlow`)
and the credential record it produces live in
:mod:`clio_agent.providers.chatgpt.login_flow` -- split out to keep this
module under the file-size ratchet.

No Codex CLI or Codex SDK is involved anywhere in this module: CLIO runs its
own OAuth flow, direct from this process, and never reads or writes
``~/.codex/auth.json`` (A.4).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from clio_agent.providers.chatgpt import constants as c

logger = logging.getLogger(__name__)

__all__ = [
    "DeviceLogin",
    "DeviceLoginUnavailableError",
    "LoopbackBindError",
    "LoopbackListener",
    "OAuthError",
    "PendingBrowserLogin",
    "PkcePair",
    "StateMismatchError",
    "TokenExchangeError",
    "TokenResponse",
    "decode_account_id",
    "exchange_code",
    "generate_pkce",
    "parse_paste",
    "poll_device_login",
    "refresh_token",
    "start_device_login",
]


class OAuthError(RuntimeError):
    """Base class for every ChatGPT OAuth failure."""


class StateMismatchError(OAuthError):
    """The redirect's ``state`` does not match the one this flow generated."""


class TokenExchangeError(OAuthError):
    """The token endpoint returned an error, or an incomplete token response."""


class LoopbackBindError(OAuthError):
    """The fixed loopback port could not be bound (e.g. the user's own Codex
    CLI is logging in at the same moment). Never fatal -- callers fall back
    to paste-only (A.3 Method 1)."""


class DeviceLoginUnavailableError(OAuthError):
    """The device-code endpoint is not enabled for this client (404)."""


# ---------------------------------------------------------------------------
# PKCE + authorize URL (A.3 Method 1 step 1-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PkcePair:
    """A generated PKCE verifier/challenge pair plus this flow's ``state``."""

    verifier: str
    challenge: str
    state: str


def generate_pkce() -> PkcePair:
    """Generate a PKCE verifier (43-128 URL-safe chars) + S256 challenge + state.

    ``verifier`` is base64url(secrets.token_bytes(96)) with padding stripped,
    which is 128 characters -- the maximum RFC 7636 allows and comfortably
    inside the 43-128 range. ``state`` is 16 random bytes, hex-encoded (A.3).
    """

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    state = secrets.token_hex(16)
    return PkcePair(verifier=verifier, challenge=challenge, state=state)


def build_authorize_url(pkce: PkcePair) -> str:
    """Build the browser authorize URL with every A.3 query parameter."""

    params = {
        "response_type": "code",
        "client_id": c.CLIENT_ID,
        "redirect_uri": c.REDIRECT_URI,
        "scope": c.SCOPE,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": pkce.state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": c.ORIGINATOR,
    }
    return f"{c.AUTHORIZE_URL}?{urlencode(params)}"


@dataclass(frozen=True)
class PendingBrowserLogin:
    """A started browser-login flow: what the client needs to render."""

    authorization_url: str
    state: str
    verifier: str
    loopback: bool


# ---------------------------------------------------------------------------
# Method 1: loopback listener
# ---------------------------------------------------------------------------

_CLOSE_PAGE = (
    "<html><head><title>ChatGPT sign-in</title></head>"
    "<body><p>Signed in. You can close this window and return to CLIO.</p></body></html>"
)
_ERROR_PAGE = (
    "<html><head><title>ChatGPT sign-in</title></head>"
    "<body><p>Sign-in failed: {reason}. Return to CLIO and try again, or paste the "
    "redirect URL directly.</p></body></html>"
)


@dataclass
class _LoopbackResult:
    code: str = ""
    state: str = ""
    error: str = ""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles ONLY ``/auth/callback`` (A.3); anything else is a 404."""

    server: "_CallbackServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Never log query strings (they carry the authorization code).
        logger.debug("chatgpt loopback callback: %s", self.command)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlparse(self.path)
        if parsed.path != c.LOOPBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]
        if error:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ERROR_PAGE.format(reason=error).encode("utf-8"))
            self.server.result.error = error
            self.server.done.set()
            return
        if state != self.server.expected_state:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ERROR_PAGE.format(reason="state mismatch").encode("utf-8"))
            self.server.result.error = "state_mismatch"
            self.server.done.set()
            return
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ERROR_PAGE.format(reason="missing code").encode("utf-8"))
            self.server.result.error = "missing_code"
            self.server.done.set()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_CLOSE_PAGE.encode("utf-8"))
        self.server.result.code = code
        self.server.result.state = state
        self.server.done.set()


class _CallbackServer(HTTPServer):
    def __init__(self, expected_state: str) -> None:
        super().__init__((c.LOOPBACK_HOST, c.LOOPBACK_PORT), _CallbackHandler)
        self.expected_state = expected_state
        self.result = _LoopbackResult()
        self.done = threading.Event()


class LoopbackListener:
    """A best-effort HTTP listener on ``127.0.0.1:1455`` for the browser flow.

    Binding can fail (e.g. the user's own Codex CLI is mid-login on the same
    port); the caller must treat that as a typed, non-fatal
    :class:`LoopbackBindError` and fall back to paste-only (A.3). Always
    ``close()``d, including on error/cancel -- callers should use this as a
    context manager.
    """

    def __init__(self, expected_state: str) -> None:
        self._expected_state = expected_state
        self._server: _CallbackServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the loopback port and start serving in a background thread.

        Raises:
            LoopbackBindError: The fixed port could not be bound.
        """

        try:
            self._server = _CallbackServer(self._expected_state)
        except OSError as exc:
            raise LoopbackBindError(
                f"could not bind loopback callback on {c.LOOPBACK_HOST}:{c.LOOPBACK_PORT}: {exc}"
            ) from exc
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="chatgpt-oauth-loopback", daemon=True
        )
        self._thread.start()

    def wait_for_code(self, timeout: float) -> tuple[str, str]:
        """Block for the callback; return ``(code, state)``.

        Raises:
            OAuthError: The callback reported an error (bad state, missing
                code, or an OAuth ``error`` parameter), or ``timeout`` elapsed
                with no callback at all.
        """

        assert self._server is not None, "start() must be called first"  # noqa: S101
        if not self._server.done.wait(timeout=timeout):
            raise OAuthError("timed out waiting for the browser sign-in callback")
        result = self._server.result
        if result.error == "state_mismatch":
            raise StateMismatchError("the sign-in callback's state did not match")
        if result.error:
            raise OAuthError(f"sign-in callback reported an error: {result.error}")
        return result.code, result.state

    def close(self) -> None:
        """Always safe to call, including when :meth:`start` never succeeded."""

        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # noqa: BLE001 - teardown must never raise
                logger.debug("chatgpt loopback listener close failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "LoopbackListener":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Method 2: paste the redirect URL (A.3) -- four accepted forms
# ---------------------------------------------------------------------------


def parse_paste(pasted: str, *, expected_state: str | None = None) -> tuple[str, str | None]:
    """Parse a pasted redirect value into ``(code, state)``.

    Accepts, in order:
      1. a full URL (``code``/``state`` read from the query string)
      2. ``code#state``
      3. a raw query string containing ``code=``
      4. a bare code

    Args:
        pasted: The user-supplied text.
        expected_state: When given and the parsed value carries a ``state``,
            it must match, or :class:`StateMismatchError` is raised.

    Returns:
        ``(code, state)`` -- ``state`` is ``None`` when the pasted form (a
        bare code) carried none.

    Raises:
        OAuthError: The pasted text carries no recognizable code.
        StateMismatchError: A present state does not match ``expected_state``.
    """

    text = pasted.strip()
    if not text:
        raise OAuthError("paste the redirect URL, or the authorization code, from the browser")

    code: str | None = None
    state: str | None = None

    parsed = urlparse(text)
    if parsed.scheme and parsed.query:
        # Form 1: a full URL.
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0] or None
        state = (query.get("state") or [""])[0] or None
    elif "code=" in text and "&" in text or (not parsed.scheme and "code=" in text):
        # Form 3: a raw query string.
        query = parse_qs(text.lstrip("?"))
        code = (query.get("code") or [""])[0] or None
        state = (query.get("state") or [""])[0] or None
    elif "#" in text:
        # Form 2: code#state.
        code, _, state = text.partition("#")
        code = code.strip() or None
        state = state.strip() or None
    else:
        # Form 4: a bare code.
        code = text

    if not code:
        raise OAuthError("could not find an authorization code in the pasted text")
    if expected_state and state and state != expected_state:
        raise StateMismatchError("the pasted redirect's state did not match")
    return code, state


# ---------------------------------------------------------------------------
# Method 3: device code (headless) (A.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceLogin:
    """A started device-code login (A.3 Method 3)."""

    device_auth_id: str
    user_code: str
    verification_url: str
    interval_s: float


def start_device_login(*, client: httpx.Client | None = None) -> DeviceLogin:
    """POST the device-usercode request; return the code the user must enter.

    Raises:
        DeviceLoginUnavailableError: The endpoint 404s (device login is not
            enabled for this client) -- the caller should tell the user to
            use browser login instead.
        OAuthError: Any other non-2xx response, or a malformed body.
    """

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(c.DEVICE_USERCODE_URL, json={"client_id": c.CLIENT_ID})
    except httpx.HTTPError as exc:
        if owns_client:
            client.close()
        raise OAuthError(f"could not reach the device sign-in endpoint: {exc}") from exc
    if response.status_code == 404:
        if owns_client:
            client.close()
        raise DeviceLoginUnavailableError(
            "device sign-in is not enabled for this account/client; use browser sign-in instead"
        )
    if response.status_code >= 400:
        if owns_client:
            client.close()
        raise OAuthError(
            f"device sign-in start failed ({response.status_code}): {response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        if owns_client:
            client.close()
        raise OAuthError(f"device sign-in start returned a non-JSON body: {exc}") from exc
    if owns_client:
        client.close()
    device_auth_id = str(payload.get("device_auth_id") or "")
    user_code = str(payload.get("user_code") or "")
    if not device_auth_id or not user_code:
        raise OAuthError("device sign-in start response is missing device_auth_id/user_code")
    # `interval` may come back as a string (A.3) -- parse defensively.
    interval_raw = payload.get("interval", 5)
    try:
        interval_s = max(c.DEVICE_MIN_INTERVAL_S, float(interval_raw))
    except (TypeError, ValueError):
        interval_s = 5.0
    return DeviceLogin(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verification_url=c.DEVICE_VERIFY_URL,
        interval_s=interval_s,
    )


@dataclass(frozen=True)
class _DevicePollResult:
    authorization_code: str
    code_verifier: str


def poll_device_login(
    login: DeviceLogin,
    *,
    timeout_s: float = c.DEVICE_TIMEOUT_S,
    sleep: Any = time.sleep,
    client: httpx.Client | None = None,
) -> _DevicePollResult:
    """Poll the device-token endpoint until the user completes sign-in.

    Args:
        login: The result of :func:`start_device_login`.
        timeout_s: Overall wall-clock budget (A.3: up to 15 minutes).
        sleep: Injected sleep function (tests pass a no-op/fake clock).
        client: Optional shared HTTP client (tests only).

    Returns:
        The authorization code plus its ``code_verifier`` (device login mints
        its own PKCE pair server-side; CLIO never generates one for this
        flow).

    Raises:
        OAuthError: The poll timed out, or the backend returned an
            unrecognized error.
    """

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    interval_s = login.interval_s
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            sleep(interval_s)
            try:
                response = client.post(
                    c.DEVICE_TOKEN_URL,
                    json={"device_auth_id": login.device_auth_id, "user_code": login.user_code},
                )
            except httpx.HTTPError as exc:
                raise OAuthError(f"device sign-in poll failed: {exc}") from exc
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise OAuthError(
                        f"device sign-in poll returned a non-JSON body: {exc}"
                    ) from exc
                authorization_code = str(payload.get("authorization_code") or "")
                code_verifier = str(payload.get("code_verifier") or "")
                if not authorization_code or not code_verifier:
                    raise OAuthError(
                        "device sign-in poll succeeded but is missing "
                        "authorization_code/code_verifier"
                    )
                return _DevicePollResult(
                    authorization_code=authorization_code, code_verifier=code_verifier
                )
            if response.status_code in (403, 404):
                continue  # pending -- keep polling
            try:
                body = response.json()
            except ValueError:
                body = {}
            error_code = str(body.get("error") or "")
            if error_code == "deviceauth_authorization_pending":
                continue
            if error_code == "slow_down":
                interval_s *= 1.5
                continue
            raise OAuthError(
                f"device sign-in poll failed ({response.status_code}): {response.text[:300]}"
            )
        raise OAuthError(f"device sign-in timed out after {timeout_s}s")
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Code exchange + refresh (all methods) (A.3, A.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenResponse:
    """A validated token-endpoint response."""

    access_token: str
    refresh_token: str
    expires_in: int


def _validate_token_payload(payload: dict[str, Any], *, context: str) -> TokenResponse:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    missing = [
        name
        for name, value in (
            ("access_token", access_token),
            ("refresh_token", refresh_token),
            ("expires_in", expires_in),
        )
        if not value and value != 0
    ]
    if missing:
        raise TokenExchangeError(f"{context} response is missing required field(s): {missing}")
    assert expires_in is not None  # noqa: S101 - validated via `missing` above
    return TokenResponse(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_in=int(expires_in),
    )


def exchange_code(
    *, code: str, code_verifier: str, redirect_uri: str, client: httpx.Client | None = None
) -> TokenResponse:
    """Exchange an authorization code for tokens (A.3 "Code exchange (all methods)")."""

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(
            c.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": c.CLIENT_ID,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"token exchange request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    if response.status_code >= 400:
        raise TokenExchangeError(
            f"token exchange failed ({response.status_code}): {response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenExchangeError(f"token exchange returned a non-JSON body: {exc}") from exc
    return _validate_token_payload(payload, context="token exchange")


def refresh_token(refresh: str, *, client: httpx.Client | None = None) -> TokenResponse:
    """Refresh an access token (A.4). Treat rotating refresh tokens as normal."""

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        response = client.post(
            c.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": c.CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"token refresh request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    if response.status_code >= 400:
        raise TokenExchangeError(
            f"token refresh failed ({response.status_code}): {response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TokenExchangeError(f"token refresh returned a non-JSON body: {exc}") from exc
    # A refresh response may omit refresh_token when the server chose not to
    # rotate it; treat the OLD refresh token as still valid in that case
    # rather than failing validation on a field the server had no obligation
    # to repeat.
    payload.setdefault("refresh_token", refresh)
    return _validate_token_payload(payload, context="token refresh")


def decode_account_id(access_token: str) -> str:
    """Decode the access token's JWT payload and return ``chatgpt_account_id``.

    No signature verification is performed (A.3: "no signature check
    needed") -- CLIO trusts the token because it just received it directly
    from ``TOKEN_URL`` over TLS; this is a local, offline decode of a claim
    already inside a token CLIO holds, not an authorization decision made
    from an untrusted token.

    Raises:
        OAuthError: The token is malformed, or the claim/account id is absent
            (A.3: "If it's missing, login failed").
    """

    parts = access_token.split(".")
    if len(parts) != 3:
        raise OAuthError("access token is not a JWT (expected 3 dot-separated parts)")
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes)
    except (ValueError, TypeError) as exc:
        raise OAuthError(f"could not decode the access token payload: {exc}") from exc
    auth_claim = payload.get(c.JWT_AUTH_CLAIM)
    account_id = (
        (auth_claim or {}).get("chatgpt_account_id") if isinstance(auth_claim, dict) else None
    )
    if not account_id:
        raise OAuthError(
            f"access token is missing {c.JWT_AUTH_CLAIM!r}.chatgpt_account_id -- login failed"
        )
    return str(account_id)
