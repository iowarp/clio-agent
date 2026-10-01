"""The Codex provider: Codex DIRECT, a DSPy 3.4 engine built by ``create_lm``.

It sends lm15's Responses payload to the Codex backend (``chatgpt.com/backend-api``)
over a kept WebSocket with delta continuation, signed in with CLIO's own OAuth
credential or, without one, the local Codex CLI login (``$CODEX_HOME/auth.json``,
default ``~/.codex``).

Submodules:
    constants: every literal endpoint/header/timeout value, the model-string
        prefix and the catalog transport label (A.2).
    oauth: PKCE, the three login methods' mechanics, code exchange, refresh, JWT decode (A.3/A.4).
    login_flow: the credential record + the stateful login-flow orchestrator that
        drives the generic start/complete/status/logout auth API.
    credentials: the durable per-machine credential store (A.4).
    direct_engine: the engine (lm15 wire, kept WebSocket, delta continuation).
    audit: stream-audit instrumentation for the engine's WebSocket calls.
    model_list: the account's live model list from the Codex backend.
    errors: typed errors + retry/terminal classification (A.7).
"""

from __future__ import annotations

from clio_agent.providers.codex.constants import LITELLM_PROVIDER, PROVIDER_ID, PROVIDER_LABEL

__all__ = ["LITELLM_PROVIDER", "PROVIDER_ID", "PROVIDER_LABEL"]
