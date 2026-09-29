"""The provider keys of clio's own engine transports (Claude Code, Codex direct/SDK).

They are clio's transports (DSPy 3.4 engines), not LiteLLM providers: LiteLLM knows
nothing true about what they accept. Consumers that would ask LiteLLM about a provider
(``capabilities/endpoint.py``) consult this set instead, so the answer never depends
on LiteLLM's registration state.
"""

from __future__ import annotations

from clio_agent.providers.codex.constants import LITELLM_PROVIDER as CODEX_DIRECT_PROVIDER
from clio_agent.providers.codex.constants import LITELLM_PROVIDER_SDK as CODEX_SDK_PROVIDER

CLAUDE_CODE_PROVIDER = "claude_code"

CLIO_CUSTOM_LITELLM_PROVIDERS: frozenset[str] = frozenset(
    {CLAUDE_CODE_PROVIDER, CODEX_DIRECT_PROVIDER, CODEX_SDK_PROVIDER}
)

__all__ = [
    "CLAUDE_CODE_PROVIDER",
    "CLIO_CUSTOM_LITELLM_PROVIDERS",
    "CODEX_DIRECT_PROVIDER",
    "CODEX_SDK_PROVIDER",
]
