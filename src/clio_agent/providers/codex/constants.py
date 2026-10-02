"""Wire constants for the direct Codex subscription provider (Part A).

OpenAI has no third-party OAuth program for Codex subscriptions. Every
harness that offers "sign in with Codex" (OpenCode, pi, Cline, Hermes)
reuses the Codex CLI's own public OAuth client and calls the Codex backend at
``chatgpt.com`` directly -- never ``api.openai.com``. Usage counts against the
user's Codex plan limits. This is the single owner of every literal
endpoint/header/timeout value the rest of :mod:`clio_agent.providers.codex`
uses, so a value never drifts between the oauth/, credentials/, responses/,
and transport modules.

No accretion: this module is a leaf -- constants only, no logic.
"""

from __future__ import annotations

#: Provider id/label used across the catalog, routes, config, and credential store.
#: Users know this subscription as "Codex" -- the id and display name say so too.
PROVIDER_ID = "codex"
PROVIDER_LABEL = "Codex"

#: The direct transport's model-string prefix (``codex_direct/<model>``) and the
#: provider key capability lookups use. Kept DELIBERATELY DISTINCT from
#: ``PROVIDER_ID`` ("codex"): LiteLLM ships a native "chatgpt"/device-code provider,
#: and a transport name that collides with a LiteLLM-native one once silently
#: intercepted every turn; a separate wire name can never collide again.
LITELLM_PROVIDER = "codex_direct"


#: The Codex CLI's public OAuth client id. Not a secret -- every open-source
#: harness that reuses this login flow (pi, OpenCode, Cline) ships the same
#: value; the security boundary is PKCE + the fixed loopback redirect, not
#: client-id confidentiality.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_BASE = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth/authorize"
TOKEN_URL = f"{AUTH_BASE}/oauth/token"
#: Fixed by the CLIENT_ID's client registration -- cannot be changed per
#: install. A CLIO-owned loopback listener binds this exact port for Method 1.
REDIRECT_URI = "http://localhost:1455/auth/callback"
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 1455
LOOPBACK_PATH = "/auth/callback"
SCOPE = "openid profile email offline_access"
#: JWT claim namespace holding ``chatgpt_account_id`` on the access token.
JWT_AUTH_CLAIM = "https://api.openai.com/auth"

# ---------------------------------------------------------------------------
# Device code (headless / clio-relay on HPC login nodes)
# ---------------------------------------------------------------------------
DEVICE_USERCODE_URL = f"{AUTH_BASE}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{AUTH_BASE}/api/accounts/deviceauth/token"
DEVICE_VERIFY_URL = f"{AUTH_BASE}/codex/device"
DEVICE_REDIRECT_URI = f"{AUTH_BASE}/deviceauth/callback"
DEVICE_TIMEOUT_S = 15 * 60
#: Floor for the poll interval, applied whenever the backend reports (or
#: omits) something smaller -- protects against a misbehaving backend turning
#: this into a tight poll loop.
DEVICE_MIN_INTERVAL_S = 1.0
DEVICE_AUTHORIZATION_PENDING_ERROR = "deviceauth_authorization_pending"
DEVICE_SLOW_DOWN_ERROR = "slow_down"

# ---------------------------------------------------------------------------
# Codex backend (the actual inference transport)
# ---------------------------------------------------------------------------
CODEX_BASE = "https://chatgpt.com/backend-api"
CODEX_HTTP_URL = f"{CODEX_BASE}/codex/responses"
CODEX_WS_URL = "wss://chatgpt.com/backend-api/codex/responses"
#: The account model list the official Codex CLI reads (``codex-rs/codex-api``
#: ``ModelsClient``: ``GET {base}/models?client_version=<v>``). The backend gates
#: each model on ``minimal_client_version``, so the version sent decides which
#: models come back.
CODEX_MODELS_URL = f"{CODEX_BASE}/codex/models"
#: The distribution whose version is the Codex client version CLIO presents to
#: the backend's model list (which gates each model on ``minimal_client_version``).
#: Bumping the pin in pyproject.toml is the one knob.
CODEX_CLIENT_DISTRIBUTION = "openai-codex-cli-bin"
ORIGINATOR = "clio"
OPENAI_BETA_SSE = "responses=experimental"
OPENAI_BETA_WEBSOCKETS = "responses_websockets=2026-02-06"

# ---------------------------------------------------------------------------
# Credential refresh (A.4)
# ---------------------------------------------------------------------------
#: Proactive refresh window: refresh when fewer than this many ms remain.
REFRESH_MARGIN_MS = 5 * 60 * 1000

# ---------------------------------------------------------------------------
# WebSocket session pool lifetime (A.6)
# ---------------------------------------------------------------------------
WS_IDLE_CLOSE_S = 5 * 60
WS_MAX_AGE_S = 55 * 60
#: The opening handshake's bound; a handshake that times out is retried once with
#: WS_CONNECT_RETRY_TIMEOUT_S before it is reported "slow or unresponsive" (#1577).
WS_CONNECT_TIMEOUT_S = 15.0
WS_CONNECT_RETRY_TIMEOUT_S = 45.0
#: Keepalive, explicit rather than websockets' implicit defaults: a ping every 20 s, and
#: 60 s for its pong -- a busy event loop reads the pong late, and a keepalive miss
#: closes a reply that is still streaming (1011).
WS_PING_INTERVAL_S = 20.0
WS_PING_TIMEOUT_S = 60.0

# ---------------------------------------------------------------------------
# Retry / error handling (A.7)
# ---------------------------------------------------------------------------
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_BASE_DELAY_MS = 1000
RETRY_MAX_DELAY_MS = 60_000
DEFAULT_MAX_RETRIES = 3

#: A 429 whose body mentions one of these is the account's plan window, not a
#: transient rate limit -- terminal, never retried (A.7).
USAGE_LIMIT_MARKERS = (
    "usage limit",
    "quota exceeded",
    "insufficient_quota",
    "billing",
    "monthly usage limit",
    "out of budget",
    "available balance",
)

PREVIOUS_RESPONSE_NOT_FOUND_CODE = "previous_response_not_found"
WEBSOCKET_CONNECTION_LIMIT_REACHED_CODE = "websocket_connection_limit_reached"

#: Typed reasons for the no-silent-fallback ground rule -- every WS->SSE
#: degradation records one of these (gact/streaming.py stream_fallback model).
REASON_LOOPBACK_BIND_FAILED = "codex_loopback_bind_failed"
REASON_DEVICE_LOGIN_UNAVAILABLE = "codex_device_login_unavailable"
REASON_WS_PRESTREAM_FAILURE = "codex_ws_prestream_failure"
REASON_WS_CONNECTION_LIMIT = "codex_ws_connection_limit_reached"
REASON_WS_MIDSTREAM_FAILURE = "codex_ws_midstream_failure"
REASON_PLAN_LIMIT = "codex_plan_limit"
REASON_AUTH_REFRESH_FAILED = "codex_auth_refresh_failed"

__all__ = [
    "AUTHORIZE_URL",
    "AUTH_BASE",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "CLIENT_ID",
    "CODEX_BASE",
    "CODEX_CLIENT_DISTRIBUTION",
    "CODEX_HTTP_URL",
    "CODEX_MODELS_URL",
    "CODEX_WS_URL",
    "DEFAULT_MAX_RETRIES",
    "DEVICE_AUTHORIZATION_PENDING_ERROR",
    "DEVICE_REDIRECT_URI",
    "DEVICE_SLOW_DOWN_ERROR",
    "DEVICE_TIMEOUT_S",
    "DEVICE_TOKEN_URL",
    "DEVICE_USERCODE_URL",
    "DEVICE_VERIFY_URL",
    "JWT_AUTH_CLAIM",
    "LITELLM_PROVIDER",
    "LOOPBACK_HOST",
    "LOOPBACK_PATH",
    "LOOPBACK_PORT",
    "OPENAI_BETA_SSE",
    "OPENAI_BETA_WEBSOCKETS",
    "ORIGINATOR",
    "PREVIOUS_RESPONSE_NOT_FOUND_CODE",
    "REASON_AUTH_REFRESH_FAILED",
    "REASON_DEVICE_LOGIN_UNAVAILABLE",
    "REASON_LOOPBACK_BIND_FAILED",
    "REASON_PLAN_LIMIT",
    "REASON_WS_CONNECTION_LIMIT",
    "REASON_WS_MIDSTREAM_FAILURE",
    "REASON_WS_PRESTREAM_FAILURE",
    "REDIRECT_URI",
    "REFRESH_MARGIN_MS",
    "RETRYABLE_STATUS_CODES",
    "RETRY_BASE_DELAY_MS",
    "RETRY_MAX_DELAY_MS",
    "SCOPE",
    "TOKEN_URL",
    "USAGE_LIMIT_MARKERS",
    "WEBSOCKET_CONNECTION_LIMIT_REACHED_CODE",
    "WS_CONNECT_TIMEOUT_S",
    "WS_IDLE_CLOSE_S",
    "WS_MAX_AGE_S",
]
