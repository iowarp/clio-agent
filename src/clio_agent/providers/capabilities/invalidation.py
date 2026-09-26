"""The capability-record store, with a separate lifetime per record (brief 5.6).

A process-global store for the latest known
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities`,
:class:`~clio_agent.providers.capabilities.records.EndpointCapabilities` and
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities` --
the same "module-level dict, one writer per key" shape already used by
:mod:`clio_agent.providers.handshake.cache` for whole reports. Each record
carries its OWN identity key (model records by
:data:`~clio_agent.providers.identity.ModelKey`; endpoint/deployment records by
:data:`~clio_agent.providers.identity.EndpointKey` /
:data:`~clio_agent.providers.identity.DeploymentKey`), so writing a new
deployment fact never touches the model or endpoint record it links to, and a
model record shared by ten deployments is written once.

A record's own :attr:`fingerprint <...EndpointCapabilities.fingerprint>`
(endpoint/deployment) is the brief 5.6 invalidation key -- when a live probe's
fingerprint no longer matches what is stored, the writer is the one recomputing
it (an adapter re-deriving ``build_info``/``/api/version``/the loaded-model
digest); this module only stores what it is given and lets
:mod:`clio_agent.providers.capabilities.accessor` decide, from the CURRENT
fingerprint, whether a cached effective view is stale. Model records have no
single version field yet (P4a: the overlay/HF layers that would supply one are
P6/P4b) so their staleness is judged structurally -- see
:func:`clio_agent.providers.capabilities.accessor._record_fingerprint`.
"""

from __future__ import annotations

from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    ModelCapabilities,
)
from clio_agent.providers.identity import DeploymentKey, EndpointKey, ModelKey

_model_records: dict[ModelKey, ModelCapabilities] = {}
_endpoint_records: dict[EndpointKey, EndpointCapabilities] = {}
_deployment_records: dict[DeploymentKey, DeploymentCapabilities] = {}


def record_model_capabilities(capabilities: ModelCapabilities) -> None:
    """Store the latest known facts for one model, keyed by its own model_key."""
    _model_records[capabilities.model_key] = capabilities


def record_endpoint_capabilities(capabilities: EndpointCapabilities) -> None:
    """Store the latest known facts for one endpoint, keyed by ``(provider_id, api_base)``."""
    _endpoint_records[(capabilities.provider_id, capabilities.api_base)] = capabilities


def record_deployment_capabilities(capabilities: DeploymentCapabilities) -> None:
    """Store the latest known facts for one deployment, keyed by ``(provider_id, api_base, model_id)``."""
    _deployment_records[
        (capabilities.provider_id, capabilities.api_base, capabilities.model_id)
    ] = capabilities


def get_model_capabilities(model_key: ModelKey | None) -> ModelCapabilities | None:
    """Return the stored model record, or ``None`` when nothing is known yet."""
    if not model_key:
        return None
    return _model_records.get(model_key)


def get_endpoint_capabilities(key: EndpointKey) -> EndpointCapabilities | None:
    """Return the stored endpoint record, or ``None`` when nothing is known yet."""
    return _endpoint_records.get(key)


def get_deployment_capabilities(key: DeploymentKey) -> DeploymentCapabilities | None:
    """Return the stored deployment record, or ``None`` when nothing is known yet."""
    return _deployment_records.get(key)


def invalidate_model(model_key: ModelKey) -> None:
    """Drop one model record (e.g. an overlay/HF/catalog version changed)."""
    _model_records.pop(model_key, None)


def invalidate_endpoint(key: EndpointKey) -> None:
    """Drop one endpoint record AND every deployment record under it.

    A server fingerprint change (a llama.cpp restart with new ``build_info``, a
    vLLM upgrade) invalidates not just what the software accepts but every
    deployment fact recorded against the old process, so both are dropped
    together rather than leaving stale deployment rows keyed to a dead
    endpoint identity.
    """
    _endpoint_records.pop(key, None)
    provider_id, api_base = key
    stale_deployments = [
        dep_key
        for dep_key in _deployment_records
        if dep_key[0] == provider_id and dep_key[1] == api_base
    ]
    for dep_key in stale_deployments:
        _deployment_records.pop(dep_key, None)


def invalidate_deployment(key: DeploymentKey) -> None:
    """Drop one deployment record (e.g. the loaded model changed)."""
    _deployment_records.pop(key, None)


def invalidate_provider(provider_id: str) -> None:
    """Drop every endpoint/deployment record for ``provider_id`` (any api_base).

    Model records are untouched -- they are not owned by any one provider.
    """
    stale_endpoints = [end_key for end_key in _endpoint_records if end_key[0] == provider_id]
    for end_key in stale_endpoints:
        invalidate_endpoint(end_key)
    stale_deployments = [dep_key for dep_key in _deployment_records if dep_key[0] == provider_id]
    for dep_key in stale_deployments:
        _deployment_records.pop(dep_key, None)


def clear_all() -> None:
    """Drop every stored record. Test-only; production code never needs a full wipe."""
    _model_records.clear()
    _endpoint_records.clear()
    _deployment_records.clear()


__all__ = [
    "clear_all",
    "get_deployment_capabilities",
    "get_endpoint_capabilities",
    "get_model_capabilities",
    "invalidate_deployment",
    "invalidate_endpoint",
    "invalidate_model",
    "invalidate_provider",
    "record_deployment_capabilities",
    "record_endpoint_capabilities",
    "record_model_capabilities",
]
