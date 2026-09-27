"""Plain, provider-named reasons for a handshake that could not reach its server.

A transport failure used to reach the catalog as the raw exception text
(``ConnectError: All connection attempts failed``). Every local provider that
was down then carried the SAME sentence, naming neither the provider nor the
address, so in the model picker it read as one provider's error showing up
under whichever provider was selected. The reason now says which server and
where, with a typed code in front that the UI strips:

- ``server_not_running`` -- nothing accepted the connection (refused, or the
  host did not resolve). For a server the person runs themselves (LM Studio,
  Ollama, llama.cpp, vLLM, a saved server): "LM Studio isn't running at
  127.0.0.1:1234." For a hosted API it is a connection problem on this side:
  "Couldn't connect to OpenAI API at api.openai.com."
- ``server_not_answering`` -- the connection or the reply timed out.
- ``server_unreachable`` -- any other transport failure, with its detail.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

SERVER_NOT_RUNNING = "server_not_running"
SERVER_NOT_ANSWERING = "server_not_answering"
SERVER_UNREACHABLE = "server_unreachable"


def server_address(api_base: str) -> str:
    """The ``host:port`` a person recognises for ``api_base`` (the URL itself if it has none)."""

    netloc = urlsplit(api_base).netloc
    return netloc or api_base


def _provider_facts(provider_id: str) -> tuple[str, bool]:
    """``(label, self_hosted)`` for ``provider_id``.

    A registry row that needs no API key and signs with no host credentials is
    a server the person runs. An id with no registry row is a saved server the
    person added, so it is self-hosted and named by its id.
    """

    from clio_agent.providers import host_credentials  # noqa: PLC0415
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider = get_provider(provider_id)
    if provider is None:
        return provider_id, True
    self_hosted = not provider.requires_api_key and not host_credentials.chain_for(provider_id)
    return provider.label, self_hosted


def unreachable_reason(provider_id: str, api_base: str, exc: BaseException) -> tuple[str, str]:
    """Return ``(error_code, reason)`` for a transport failure reaching ``api_base``.

    Args:
        provider_id: The provider (or saved server) the handshake ran for.
        api_base: The address it tried.
        exc: The transport exception.

    Returns:
        The typed code and the reason string ``"<code>: <plain sentence>"``.
    """

    label, self_hosted = _provider_facts(provider_id)
    address = server_address(api_base)
    if isinstance(exc, httpx.ConnectError):
        sentence = (
            f"{label} isn't running at {address}."
            if self_hosted
            else f"Couldn't connect to {label} at {address}. Check this computer's network connection."
        )
        return SERVER_NOT_RUNNING, f"{SERVER_NOT_RUNNING}: {sentence}"
    if isinstance(exc, httpx.TimeoutException):
        sentence = f"{label} at {address} didn't answer in time."
        return SERVER_NOT_ANSWERING, f"{SERVER_NOT_ANSWERING}: {sentence}"
    detail = str(exc).strip() or type(exc).__name__
    sentence = f"Couldn't reach {label} at {address} ({detail})."
    return SERVER_UNREACHABLE, f"{SERVER_UNREACHABLE}: {sentence}"


__all__ = [
    "SERVER_NOT_ANSWERING",
    "SERVER_NOT_RUNNING",
    "SERVER_UNREACHABLE",
    "server_address",
    "unreachable_reason",
]
