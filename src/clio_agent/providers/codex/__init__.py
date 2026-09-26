"""The Codex provider: TWO transports of the SAME catalog entry (S1b).

``sdk`` restores the official ``openai_codex`` Python SDK against the user's
OWN ``CODEX_HOME`` (no CLIO-held credential, no CLI, the Codex runtime owns
its own login/refresh). ``direct`` signs in with a CLIO-held OAuth credential
and calls the Codex backend (``chatgpt.com/backend-api``) directly from this
process. Per the owner ruling: each transport has its own availability, and
the provider is READY when EITHER is available.

Submodules (direct transport):
    constants: every literal endpoint/header/timeout value, plus BOTH
        transports' LiteLLM wire names and catalog labels (A.2).
    oauth: PKCE, the three login methods' mechanics, code exchange, refresh, JWT decode (A.3/A.4).
    login_flow: the credential record + the stateful login-flow orchestrator that
        drives the generic start/complete/status/logout auth API.
    credentials: the durable per-machine credential store (A.4).
    responses: OpenAI-chat-message <-> Responses-API conversion (A.5).
    stream_events: raw Responses event -> normalized event mapping (A.5).
    transport_sse: the HTTP/SSE transport (A.5).
    transport_ws: the WebSocket transport with delta continuation (A.6).
    sessions: per-CLIO-session state shared by both transports.
    errors: typed errors + retry/terminal classification, shared by both
        transports (A.7 + the SDK's own ``CodexSDKError``).
    litellm_adapter: the LiteLLM ``CustomLLM`` registered as ``codex_direct``.

Submodules (sdk transport, S1b restore):
    sdk_client: the persistent official-SDK client + turn stream bridge.
    sdk_transport: the LiteLLM ``CustomLLM`` registered as ``codex_sdk``.
    sdk_discovery: installed/signed-in/live-model-list probe, asked of the
        SDK itself -- never a read of ``~/.codex/auth.json``.
    sdk_audit: stream-audit instrumentation mirroring the direct transport's.
"""

from __future__ import annotations

from clio_agent.providers.codex.constants import LITELLM_PROVIDER, PROVIDER_ID, PROVIDER_LABEL

__all__ = ["LITELLM_PROVIDER", "PROVIDER_ID", "PROVIDER_LABEL"]
