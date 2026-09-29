"""The Codex provider: TWO transports of the SAME catalog entry (S1b).

Both are DSPy 3.4 engines (``dspy.LM(engine=...)``) built by ``create_lm``.
``sdk`` runs the official ``openai_codex`` Python SDK against the user's OWN
``CODEX_HOME`` (the Codex runtime owns its own login/refresh). ``direct`` sends
lm15's Responses payload to the Codex backend (``chatgpt.com/backend-api``) over a
kept WebSocket with delta continuation, signed in with CLIO's own OAuth credential
or, without one, the local Codex CLI login. Per the owner ruling: each transport has
its own availability, and the provider is READY when EITHER is available.

Submodules (direct transport):
    constants: every literal endpoint/header/timeout value, plus BOTH
        transports' model-string prefixes and catalog labels (A.2).
    oauth: PKCE, the three login methods' mechanics, code exchange, refresh, JWT decode (A.3/A.4).
    login_flow: the credential record + the stateful login-flow orchestrator that
        drives the generic start/complete/status/logout auth API.
    credentials: the durable per-machine credential store (A.4).
    direct_engine: the engine (lm15 wire, kept WebSocket, delta continuation).
    errors: typed errors + retry/terminal classification, shared by both
        transports (A.7 + the SDK's own ``CodexSDKError``).

Submodules (sdk transport, S1b restore):
    sdk_client: the persistent official-SDK client + turn stream bridge.
    sdk_engine: the engine over the SDK client.
    sdk_discovery: installed/signed-in/live-model-list probe, asked of the SDK itself.
    sdk_audit: stream-audit instrumentation.
"""

from __future__ import annotations

from clio_agent.providers.codex.constants import LITELLM_PROVIDER, PROVIDER_ID, PROVIDER_LABEL

__all__ = ["LITELLM_PROVIDER", "PROVIDER_ID", "PROVIDER_LABEL"]
