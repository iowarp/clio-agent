"""The direct Codex (Codex backend) subscription provider.

CLIO signs in with a user's Codex account and calls the Codex backend
(``chatgpt.com/backend-api``) directly from this process -- no Codex CLI, no
Codex SDK anywhere in the loop. Replaces the deleted ``openai_codex``-based
provider entirely (see ``providers/catalog.py``'s ``codex`` entry and
``lm/factory.py``).

Submodules:
    constants: every literal endpoint/header/timeout value (A.2).
    oauth: PKCE, the three login methods' mechanics, code exchange, refresh, JWT decode (A.3/A.4).
    login_flow: the credential record + the stateful login-flow orchestrator that
        drives the generic start/complete/status/logout auth API.
    credentials: the durable per-machine credential store (A.4).
    responses: OpenAI-chat-message <-> Responses-API conversion (A.5).
    stream_events: raw Responses event -> normalized event mapping (A.5).
    transport_sse: the HTTP/SSE transport (A.5).
    transport_ws: the WebSocket transport with delta continuation (A.6).
    sessions: per-CLIO-session state shared by both transports.
    errors: typed errors + retry/terminal classification (A.7).
    litellm_adapter: the LiteLLM ``CustomLLM`` registered as ``codex``.
"""

from __future__ import annotations

from clio_agent.providers.codex.constants import LITELLM_PROVIDER, PROVIDER_ID, PROVIDER_LABEL

__all__ = ["LITELLM_PROVIDER", "PROVIDER_ID", "PROVIDER_LABEL"]
