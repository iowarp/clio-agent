"""
Argonne / ALCF inference-endpoint authentication.

ALCF exposes vLLM-backed model servers (Sophia today; Polaris and
Aurora behind the same gateway) on the public internet at
``https://inference-api.alcf.anl.gov/resource_server/<system>/vllm/v1``.
Access requires a short-lived Globus Auth bearer token tied to a user
identity in an ``anl.gov`` / ``alcf.anl.gov`` domain.

This module is a thin port of the auth flow from
``alcf-agentics-workflow/remoteGlobusToAurora/src/tools/globus_interface.py``.
We kept the upstream client IDs and scope verbatim so existing user
sessions (``~/.globus/app/<client>/<app>/tokens.json``) carry over;
re-running the OAuth flow isn't required when switching from the
ALCF demo workflows to CLIO.

The module is import-safe even when ``globus-sdk`` isn't installed —
all globus calls happen behind ``_require_globus()`` which raises a
``GlobusUnavailable`` with install instructions. CLIO only imports
this file when ``CLIO_LM_PROVIDER=argonne`` (or equivalent), so users
who don't talk to ALCF never need the dep.

Public API:
    get_access_token(force_refresh=False) -> str
        Return a fresh bearer token, triggering OAuth if needed.
    check_auth_status() -> bool
        Cheap probe used by ``/doctor`` / ``GET /health``.
    authenticate(force=False) -> None
        Run the OAuth flow explicitly (e.g. from a TUI button).
    tokens_exist() -> bool
        Whether a stored refresh token exists on disk.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("clio_agent.providers.argonne_auth")

# ---------------------------------------------------------------------------
# Constants — match alcf-agentics-workflow exactly so tokens are shared.
# ---------------------------------------------------------------------------

APP_NAME = "alcf_agentics_workflow"
AUTH_CLIENT_ID = "58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944"
GATEWAY_CLIENT_ID = "681c10cc-f684-4540-bcd7-0b4df3bc26ef"
GATEWAY_SCOPE = f"https://auth.globus.org/scopes/{GATEWAY_CLIENT_ID}/action_all"
ALLOWED_DOMAINS = ["anl.gov", "alcf.anl.gov"]

TOKENS_PATH = os.path.join(
    os.path.expanduser("~"),
    ".globus",
    "app",
    AUTH_CLIENT_ID,
    APP_NAME,
    "tokens.json",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GlobusUnavailable(RuntimeError):
    """Raised when ``globus-sdk`` is not importable.

    Surfacing this as its own type lets the agent / TUI render a
    helpful "pip install clio-agent[argonne]" hint instead of a bare
    ``ModuleNotFoundError``.
    """


#: User-facing copy for the generic install route (never the raw CLI
#: pip-install hint from :func:`_require_globus` -- that stays in the
#: diagnostic detail, not the primary message).
ARGONNE_NOT_INSTALLED_MESSAGE = "ALCF sign-in support is not installed on the connected agent."
ARGONNE_INSTALL_FAILED_MESSAGE = (
    "CLIO could not install ALCF sign-in support. Check the connected agent's "
    "internet connection and try again."
)


class GlobusAuthError(RuntimeError):
    """Auth flow failed (bad domain, network, expired refresh, …)."""


@dataclass(frozen=True)
class PendingAuthentication:
    """Browser authorization flow waiting for its one-time Globus code."""

    flow_id: str
    authorization_url: str


@dataclass
class _PendingAuthenticationState:
    client: Any
    expires_at: float


_AUTH_FLOW_TTL_SECONDS = 15 * 60
_pending_authentications: dict[str, _PendingAuthenticationState] = {}
_pending_authentications_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Lazy globus-sdk loader
# ---------------------------------------------------------------------------


def _require_globus() -> Any:
    try:
        import globus_sdk  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on env
        raise GlobusUnavailable(
            "Argonne / ALCF provider requires the 'globus-sdk' package. "
            "Install with:  pip install 'clio-agent[argonne]'  "
            "(or:  pip install globus-sdk )."
        ) from exc
    return globus_sdk


def _domain_error_handler(app: Any, error: Any, *, allow_interactive: bool = True) -> None:
    """Force the user back through OAuth into an ALCF-allowed domain.

    Globus authorises arbitrary identity providers, but ALCF's
    inference gateway only honours ``anl.gov`` / ``alcf.anl.gov``
    identities. Re-driving login with ``session_required_single_domain``
    keeps the user from picking, e.g., a personal Google identity that
    the gateway will then 403.

    When ``allow_interactive`` is ``False`` (passive probes such as
    ``check_auth_status`` / the handshake), we must never pop a browser:
    instead we raise :class:`GlobusAuthError` with a structured
    ``reason=argonne_login_required`` so the caller can report
    "login required" rather than block the server on an interactive flow.
    """
    if not allow_interactive:
        logger.warning(
            "Globus auth error %r — interactive login disabled (reason=argonne_login_required)",
            error,
        )
        raise GlobusAuthError(
            "argonne login required: interactive Globus login is disabled for "
            "passive probes (reason=argonne_login_required)"
        )
    globus_sdk = _require_globus()
    logger.warning("Globus auth error %r — re-running login flow", error)
    auth_params = globus_sdk.gare.GlobusAuthorizationParameters(
        session_required_single_domain=ALLOWED_DOMAINS
    )
    app.login(auth_params=auth_params)


# ---------------------------------------------------------------------------
# UserApp construction
# ---------------------------------------------------------------------------


def _build_user_app(force: bool = False, *, allow_interactive: bool = True) -> Any:
    """Instantiate (and optionally re-login) the Globus ``UserApp``.

    The UserApp persists tokens at ``TOKENS_PATH`` (managed by Globus
    SDK). On first call without an existing token, the SDK prints a
    URL the user must visit and paste back a code; afterwards the
    refresh token keeps things going for ~6 months.

    ``allow_interactive`` is threaded into the token-validation error
    handler so passive callers (health / doctor / handshake) get a
    ``reason=argonne_login_required`` error instead of a blocking login.
    """
    globus_sdk = _require_globus()

    class _Handler:
        def __call__(self, app: Any, error: Any) -> None:
            _domain_error_handler(app, error, allow_interactive=allow_interactive)

    app = globus_sdk.UserApp(
        APP_NAME,
        client_id=AUTH_CLIENT_ID,
        scope_requirements={GATEWAY_CLIENT_ID: [GATEWAY_SCOPE]},
        config=globus_sdk.GlobusAppConfig(
            request_refresh_tokens=True,
            token_validation_error_handler=_Handler(),
        ),
    )

    if force:
        auth_params = globus_sdk.gare.GlobusAuthorizationParameters(
            session_required_single_domain=ALLOWED_DOMAINS
        )
        app.login(auth_params=auth_params)

    return app


def _get_authorizer(force: bool = False, *, allow_interactive: bool = True) -> Any:
    """Return a ``RefreshTokenAuthorizer`` bound to the gateway scope."""
    app = _build_user_app(force=force, allow_interactive=allow_interactive)
    return app.get_authorizer(GATEWAY_CLIENT_ID)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def token_paths() -> tuple[str, ...]:
    """Return token paths used by Globus SDK versions on supported platforms."""
    paths = [
        Path(TOKENS_PATH),
    ]
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            paths.append(
                Path(local_app_data) / "globus" / "app" / AUTH_CLIENT_ID / APP_NAME / "tokens.json"
            )
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data_home:
            paths.append(
                Path(xdg_data_home) / "globus" / "app" / AUTH_CLIENT_ID / APP_NAME / "tokens.json"
            )
        paths.append(
            Path.home()
            / ".local"
            / "share"
            / "globus"
            / "app"
            / AUTH_CLIENT_ID
            / APP_NAME
            / "tokens.json"
        )

    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        resolved = str(path)
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return tuple(out)


def tokens_exist() -> bool:
    """Whether stored tokens exist on disk (cheap, no globus import)."""
    return any(os.path.isfile(path) for path in token_paths())


def readiness() -> tuple[str, str, bool]:
    """Return ``(status, status_message, is_authenticated)`` for an ALCF preset.

    The one place this is computed (mirrors ``_codex_readiness`` /
    ``_claude_code_readiness`` living beside their own providers): an env
    token wins outright; otherwise missing the 'argonne' extra is
    ``install_required`` regardless of sign-in state (a stored token can
    outlive the runtime that saved it), then no stored token or a stored
    token that fails to refresh is ``auth_required``, else ``ready``.
    """
    env_token = (
        os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
        or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
    )
    if env_token:
        return "ready", "ALCF token present in environment", True
    if not sdk_available():
        return "install_required", ARGONNE_NOT_INSTALLED_MESSAGE, False
    if not tokens_exist():
        return "auth_required", "no Globus token stored; authenticate ALCF before connecting", False
    if check_auth_status():
        return "ready", "Globus token validated", True
    return (
        "auth_required",
        "stored Globus token could not be refreshed; authenticate ALCF",
        False,
    )


def sdk_available() -> bool:
    """Whether the 'argonne' extra (globus-sdk) is importable right now.

    A cheap ``find_spec`` check, never an import -- callers that need the
    module itself still go through :func:`_require_globus`. The ONE place
    this is checked from, so a provider-status computation and an auth-state
    computation can never quietly disagree about whether ALCF needs Install.
    """
    return importlib.util.find_spec("globus_sdk") is not None


def sign_out() -> None:
    """Revoke and delete every stored ALCF Globus token.

    ``UserApp.logout()`` (globus-sdk >= 4.x) revokes both the access and
    refresh token for each resource server it finds stored, then removes
    them from token storage -- the SDK's own documented sign-out primitive
    (never a hand-rolled "delete the JSON file" that skips revocation).
    Idempotent: it only acts on resource servers it actually finds token
    data for, so calling this with nothing stored (already signed out, or
    never signed in) does nothing and never raises.

    A revoke call that fails (network down, Globus unreachable) must not
    leave a locally "still signed in" credential behind, so the on-disk
    token file(s) are removed unconditionally afterward regardless of
    whether ``logout()`` completed cleanly -- and unconditionally when the
    'argonne' extra itself isn't installed (there is then no client to
    revoke through, but the stale file must still go).
    """
    try:
        app = _build_user_app(force=False, allow_interactive=False)
    except GlobusUnavailable:
        app = None
    if app is not None:
        try:
            app.logout()
        except Exception as exc:  # noqa: BLE001 - best-effort revoke, deletion still proceeds
            logger.warning("ALCF token revocation failed (removing local tokens anyway): %s", exc)
    for path in token_paths():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def get_access_token(force_refresh: bool = False, *, allow_interactive: bool = True) -> str:
    """Return a valid bearer token for the ALCF inference gateway.

    Args:
        force_refresh: Re-drive OAuth even if a stored token exists.
            Use after an explicit logout or when the user reports a
            403 they suspect is auth-related.
        allow_interactive: When ``False`` (passive probes: health /
            doctor / handshake), an expired refresh token raises
            :class:`GlobusAuthError` with ``reason=argonne_login_required``
            instead of blocking on an interactive Globus login.

    Raises:
        GlobusUnavailable: ``globus-sdk`` isn't installed.
        GlobusAuthError: OAuth flow failed (bad creds, network, …) or a
            passive probe needs an interactive login.
    """
    try:
        authorizer = _get_authorizer(force=force_refresh, allow_interactive=allow_interactive)
        # ensure_valid_token refreshes silently when within the
        # refresh-token lifetime; otherwise it triggers the
        # token_validation_error_handler we registered above.
        authorizer.ensure_valid_token()
        return authorizer.access_token
    except GlobusUnavailable:
        raise
    except Exception as exc:
        raise GlobusAuthError(f"Failed to obtain ALCF access token: {exc}") from exc


def check_auth_status() -> bool:
    """True when we can mint a token without prompting the user.

    Used by ``/doctor`` and ``GET /health`` so the TUI can render a
    "ALCF auth: ok / login required" badge without forcing every
    health check to spawn a browser.
    """
    try:
        if not tokens_exist():
            return False
        authorizer = _get_authorizer(force=False, allow_interactive=False)
        authorizer.ensure_valid_token()
        return True
    except GlobusUnavailable:
        return False
    except Exception as exc:  # noqa: BLE001 - auth probe failures are non-fatal
        logger.info("ALCF auth probe failed: %s", exc)
        return False


def begin_authentication(*, force_login: bool = False) -> PendingAuthentication:
    """Start a remote-safe Globus login and return its browser URL.

    The login client remains on the agent because its PKCE verifier is required
    to exchange the authorization code.  Only an opaque flow id and the public
    Globus URL cross the desktop API boundary.

    ``force_login`` sets Globus Auth's own ``prompt=login`` (globus-sdk >= 4.x,
    verified live: ``NativeAppAuthClient.oauth2_get_authorize_url`` takes
    ``prompt: Literal['login']``), which makes Globus require a fresh
    interactive login even if the browser still carries an existing Globus
    session cookie. This is the ONE action for
    ``argonne_reauthentication_required`` ("Sign in again"): re-using a stale
    browser session would silently reproduce the same rejected credential.
    """
    globus_sdk = _require_globus()
    client = globus_sdk.NativeAppAuthClient(
        AUTH_CLIENT_ID,
        app_name=APP_NAME,
    )
    client.oauth2_start_flow(
        requested_scopes=[GATEWAY_SCOPE, "openid"],
        refresh_tokens=True,
        prefill_named_grant=APP_NAME,
    )
    authorize_kwargs: dict[str, Any] = {"session_required_single_domain": ALLOWED_DOMAINS}
    if force_login:
        authorize_kwargs["prompt"] = "login"
    authorization_url = client.oauth2_get_authorize_url(**authorize_kwargs)
    flow_id = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _pending_authentications_lock:
        expired = [
            candidate
            for candidate, state in _pending_authentications.items()
            if state.expires_at <= now
        ]
        for candidate in expired:
            _pending_authentications.pop(candidate, None)
        _pending_authentications[flow_id] = _PendingAuthenticationState(
            client=client,
            expires_at=now + _AUTH_FLOW_TTL_SECONDS,
        )
    return PendingAuthentication(flow_id=flow_id, authorization_url=authorization_url)


def flow_is_pending(flow_id: str) -> bool:
    """Whether ``flow_id`` is a live, unexpired :func:`begin_authentication` flow.

    Used by the generic provider sign-in API's ``status`` action -- ALCF's
    flow has no async background half, so "pending" means only "still
    awaiting `complete_authentication`", never a live progress signal.
    """
    with _pending_authentications_lock:
        state = _pending_authentications.get(flow_id.strip())
    return state is not None and state.expires_at > time.monotonic()


def complete_authentication(flow_id: str, authorization_code: str) -> None:
    """Exchange a browser authorization code and persist refresh tokens.

    Args:
        flow_id: Opaque id returned by :func:`begin_authentication`.
        authorization_code: One-time code displayed by Globus Auth.

    Raises:
        GlobusAuthError: The flow expired, the code is invalid, or token storage
            failed.
    """
    normalized_flow_id = flow_id.strip()
    normalized_code = authorization_code.strip()
    if not normalized_flow_id or not normalized_code:
        raise GlobusAuthError("The Globus authorization code is required.")

    with _pending_authentications_lock:
        state = _pending_authentications.pop(normalized_flow_id, None)
    if state is None or state.expires_at <= time.monotonic():
        raise GlobusAuthError("This Globus sign-in expired. Start sign-in again.")

    try:
        response = state.client.oauth2_exchange_code_for_tokens(normalized_code)
        app = _build_user_app(force=False, allow_interactive=False)
        app.token_storage.store_token_response(response)
        authorizer = app.get_authorizer(GATEWAY_CLIENT_ID)
        authorizer.ensure_valid_token()
    except Exception as exc:
        raise GlobusAuthError(f"Could not complete ALCF sign-in: {exc}") from exc


def authenticate(force: bool = False) -> None:
    """Run the OAuth flow explicitly. Wired to the CLI ``/argonne login``
    command (and equivalent TUI button). On a fresh machine this
    prints a URL the user must visit; on machines that already have
    tokens it validates or refreshes the gateway access token unless
    ``force=True`` redrives the interactive Globus login first."""
    get_access_token(force_refresh=force)


# ---------------------------------------------------------------------------
# Standalone CLI — same contract as the alcf-agentics-workflow script,
# so existing operator runbooks ('python -m … authenticate') keep working.
# ---------------------------------------------------------------------------


def _main() -> None:  # pragma: no cover - thin CLI wrapper
    import argparse

    parser = argparse.ArgumentParser(
        prog="clio-agent.providers.argonne_auth",
        description="Globus auth helper for the ALCF inference gateway",
    )
    parser.add_argument(
        "action",
        choices=["authenticate", "get-access-token", "status"],
    )
    parser.add_argument("-f", "--force", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s | %(message)s",
    )

    if args.action == "authenticate":
        authenticate(force=args.force)
        print("ALCF access token validated.")
    elif args.action == "get-access-token":
        if not tokens_exist():
            raise SystemExit("No tokens on disk; run 'authenticate' first.")
        print(get_access_token(force_refresh=args.force))
    elif args.action == "status":
        ok = check_auth_status()
        print("ok" if ok else "login required")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":  # pragma: no cover
    _main()
