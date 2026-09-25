"""Wire constants for the direct ChatGPT subscription provider (Part A).

OpenAI has no third-party OAuth program for ChatGPT subscriptions. Every
harness that offers "sign in with ChatGPT" (OpenCode, pi, Cline, Hermes)
reuses the Codex CLI's own public OAuth client and calls the Codex backend at
``chatgpt.com`` directly -- never ``api.openai.com``. Usage counts against the
user's ChatGPT plan limits. This is the single owner of every literal
endpoint/header/timeout value the rest of :mod:`clio_agent.providers.chatgpt`
uses, so a value never drifts between the oauth/, credentials/, responses/,
and transport modules.

No accretion: this module is a leaf -- constants only, no logic.
"""

from __future__ import annotations

#: Provider id/label used across the catalog, routes, config, and credential store.
PROVIDER_ID = "chatgpt"
PROVIDER_LABEL = "ChatGPT (subscription)"

#: The LiteLLM-facing custom-provider key AND model-string prefix
#: (``f"{LITELLM_PROVIDER}/cg-<model>"``, registered via
#: ``providers._cli_provider.register_custom_provider`` in
#: ``providers.chatgpt.litellm_adapter``). This is DELIBERATELY NOT
#: ``PROVIDER_ID`` ("chatgpt"): litellm ships its own native "chatgpt"
#: provider (``litellm/llms/chatgpt/`` -- a device-code OAuth client against
#: auth.openai.com), and litellm resolves a known native prefix before ever
#: consulting ``litellm.custom_provider_map``. Registering (or routing
#: ``dspy.LM``) under "chatgpt" silently hands every turn to litellm's own
#: provider instead of this module's -- it never touches
#: ``ChatGptCredentialStore``/the WS-SSE transport, and blocks on a real
#: device-code prompt. Verified against the installed litellm build:
#: ``"chatgpt" in litellm.provider_list`` is True, ``"chatgpt_direct" in
#: litellm.provider_list`` is False. Never rename this back to "chatgpt".
LITELLM_PROVIDER = "chatgpt_direct"

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
WS_CONNECT_TIMEOUT_S = 15.0

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
REASON_LOOPBACK_BIND_FAILED = "chatgpt_loopback_bind_failed"
REASON_DEVICE_LOGIN_UNAVAILABLE = "chatgpt_device_login_unavailable"
REASON_WS_PRESTREAM_FAILURE = "chatgpt_ws_prestream_failure"
REASON_WS_CONNECTION_LIMIT = "chatgpt_ws_connection_limit_reached"
REASON_WS_MIDSTREAM_FAILURE = "chatgpt_ws_midstream_failure"
REASON_PLAN_LIMIT = "chatgpt_plan_limit"
REASON_AUTH_REFRESH_FAILED = "chatgpt_auth_refresh_failed"

__all__ = [
    "AUTHORIZE_URL",
    "AUTH_BASE",
    "PROVIDER_ID",
    "PROVIDER_LABEL",
    "CLIENT_ID",
    "CODEX_BASE",
    "CODEX_HTTP_URL",
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
