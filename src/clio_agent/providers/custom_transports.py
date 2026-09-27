"""The LiteLLM custom-provider keys clio registers for its own SDK/CLI transports.

clio drives Claude Code and Codex through its own ``CustomLLM`` handlers, registered
with LiteLLM under these keys (``providers/_cli_provider.register_custom_provider``).
They are clio's transports, not LiteLLM providers: once LiteLLM has set a custom
handler up, ``get_supported_openai_params`` answers its generic OpenAI list for the
key, which says nothing about what the transport accepts. Consumers that ask LiteLLM
about a provider (``capabilities/endpoint.py``) consult this set instead of LiteLLM's
mutable registration state, so the answer never depends on what ran earlier in the
process. :func:`~clio_agent.providers._cli_provider.register_custom_provider` refuses a
key missing from this set, so a new transport cannot bypass it.
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
