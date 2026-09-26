"""Model / endpoint / deployment capability records (model-capabilities brief Parts 3-6).

clio-agent keeps three separate records instead of one flat per-model profile:

* :class:`~clio_agent.providers.capabilities.records.ModelCapabilities` --
  what the weights can do, shared by every server that runs them.
* :class:`~clio_agent.providers.capabilities.records.EndpointCapabilities` --
  what one configured server's SOFTWARE accepts.
* :class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities` --
  how a model is actually loaded on one server.

:func:`~clio_agent.providers.capabilities.accessor.get_effective_capabilities`
is the ONE place a consumer asks "what can I actually do with
``(provider_id, api_base, model_id)`` right now" -- it combines whatever the
three records currently say
(:func:`~clio_agent.providers.capabilities.combine.combine_capabilities`) and
caches the result until a contributing record changes.

Submodules:

* :mod:`.records` -- the three dataclasses plus :class:`.records.Fact`.
* :mod:`.model_sources` -- brief 5.1's source precedence for model records.
* :mod:`.endpoint` -- brief 5.2's endpoint-record resolution (LiteLLM + the
  thin dialect supplement table).
* :mod:`.link` -- brief 5.4's deployment -> model_key linking rules.
* :mod:`.combine` -- brief 5.5's pure combination rules.
* :mod:`.invalidation` -- the process-global record store (brief 5.6).
* :mod:`.accessor` -- the one cached accessor built on all of the above.
"""

from __future__ import annotations

from clio_agent.providers.capabilities.accessor import clear_cache, get_effective_capabilities
from clio_agent.providers.capabilities.combine import (
    Decision,
    EffectiveCapabilities,
    ThinkingDecision,
    combine_capabilities,
)
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    no_restriction,
    unknown,
)

__all__ = [
    "Decision",
    "DeploymentCapabilities",
    "EffectiveCapabilities",
    "EndpointCapabilities",
    "Fact",
    "ModelCapabilities",
    "ThinkingDecision",
    "ThinkingSpec",
    "clear_cache",
    "combine_capabilities",
    "get_effective_capabilities",
    "no_restriction",
    "unknown",
]
