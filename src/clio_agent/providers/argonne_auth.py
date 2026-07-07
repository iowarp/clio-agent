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

import logging
import os
import sys
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


class GlobusAuthError(RuntimeError):
    """Auth flow failed (bad domain, network, expired refresh, …)."""


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
            "Globus auth error %r — interactive login disabled "
            "(reason=argonne_login_required)",
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
    except Exception as exc:
        logger.info("ALCF auth probe failed: %s", exc)
        return False


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
