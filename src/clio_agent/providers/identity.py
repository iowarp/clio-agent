"""The three identity keys (model-capabilities brief Part 3).

``provider_kind`` chooses an API *dialect* (which wire format to speak). It
never identifies a provider, a configuration, or a cached result — nine
providers in the catalog (:mod:`clio_agent.providers.catalog`) share the kind
``"openai"`` (llama.cpp, vLLM, OpenRouter, Azure OpenAI, ...), so keying
anything by kind alone silently conflates all of them (iowarp/clio-agent
#1418: llama.cpp listed as Amazon Bedrock because both share a kind and the
kind-sorted preset list picked the wrong one).

The three keys every cache, catalog snapshot, handshake result, and message
route must use instead:

* **Endpoint** — ``(provider_id, api_base)``: one configured server.
* **Deployment** — ``(provider_id, api_base, model_id)``: one model as loaded
  on that server, ``model_id`` being the id the server itself uses on the
  wire.
* **Model** — a single ``model_key`` string (a Hugging Face repo id, a cloud
  model id, or an overlay family name): shared across every endpoint that
  serves the same weights. The model record itself lands in a later slice
  (Part 4); this module only reserves the key type so that slice has
  somewhere to plug in.

``api_base`` is always normalized (:func:`clio_agent.providers.api_base.normalize`)
before it enters a key, so cosmetically different spellings of the same
endpoint (a trailing slash, a spelled-out default port) never produce two
different cache entries for one real server.
"""

from __future__ import annotations

from clio_agent.providers.api_base import normalize

#: One configured server: a provider id plus its (normalized) api_base.
EndpointKey = tuple[str, str]

#: One model as loaded on one server: the endpoint key plus the wire model id.
DeploymentKey = tuple[str, str, str]

#: A canonical model identity (Hugging Face repo id, cloud model id, or
#: overlay family name), shared by every endpoint serving the same weights.
#: The record it keys is Part 4; this alias exists so callers can start
#: typing "the key a model record will have" ahead of that record landing.
ModelKey = str


def endpoint_key(provider_id: str, api_base: str) -> EndpointKey:
    """Build the identity key for one configured endpoint."""
    return (provider_id, normalize(api_base))


def deployment_key(provider_id: str, api_base: str, model_id: str) -> DeploymentKey:
    """Build the identity key for one model as deployed on one endpoint."""
    return (provider_id, normalize(api_base), model_id)


__all__ = [
    "DeploymentKey",
    "EndpointKey",
    "ModelKey",
    "deployment_key",
    "endpoint_key",
]
