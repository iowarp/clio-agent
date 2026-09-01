"""Closed refusal catalog for the CMF artifact-provenance provider.

A SIBLING of ``_STREAM_FALLBACK_REASON_DEFINITIONS``
(:mod:`clio_agent.gact.runtime.capabilities`) for the CMF write/query seam, and
it follows that catalog's contract exactly: a closed dict of typed reasons, each
with a ``category``, ``recovery_actions`` and a ``description``, and a payload
builder that raises :class:`ValueError` on an unknown key so a new degradation
cannot be invented at a call site.

Why the CMF seam needs its own catalog rather than a bare exception: CMF's write
path has several genuinely unsupported *combinations* (no write target
configured, a local MLMD runtime that cannot exist on this platform, an artifact
kind CMF's ingest cannot represent), and CMF's own server silently swallows
several of them -- ``cmf_merger.handle_event`` ends in ``else: pass`` for an
unknown artifact type, and ``log_model_with_version`` raises into a bare
``except Exception`` that only logs. A push therefore returns
``{"status": "success"}`` while entities are dropped. Every such case must reach
the trace as a typed reason instead of a green counter, so this catalog is the
single vocabulary for refusing a write CLIO cannot honestly perform.

Deliberately kept OUT of ``_STREAM_FALLBACK_REASON_DEFINITIONS`` and its
client-facing ``x_clio_stream_fallback_reasons`` capability (an audited closed
set of *live-streaming* fallbacks) so an unrelated provenance reason cannot
break that contract. Refusals surface through
:class:`~clio_agent.gact.provenance.protocol.ProviderHealth` (``status`` /
``last_error``, via the dispatcher's contained-emit path) and through the
``GET /v1/artifacts/{id}/lineage`` error envelope.
"""

from __future__ import annotations

from typing import Any

# The closed set. A reason is added here, with its recovery actions, or it does
# not exist -- ``cmf_refusal_payload`` rejects anything else.
CMF_REFUSAL_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "cmf_no_write_target": {
        "category": "runtime_configuration",
        "writes": False,
        "recovery_actions": ["configure_server_url", "configure_local_python"],
        "description": (
            "The CMF provider is selected but nothing tells it where to write: "
            "neither provenance.artifacts.cmf.server_url (server mode) nor "
            "provenance.artifacts.cmf.python (local worker mode) is set. Set "
            "server_url alone for the supported deployment shape."
        ),
    },
    "cmf_conflicting_write_targets": {
        "category": "runtime_configuration",
        "writes": False,
        "recovery_actions": ["unset_local_python", "unset_server_url"],
        "description": (
            "Two write targets were configured in a combination whose ordering "
            "CLIO cannot make deterministic. server_url alone (server mode) and "
            "python alone (local worker mode) are the supported declarations."
        ),
    },
    "cmf_local_runtime_unsupported_platform": {
        "category": "platform_limitation",
        "writes": False,
        "recovery_actions": ["configure_server_url"],
        "description": (
            "Local worker mode needs an interpreter that can import cmflib and "
            "ml-metadata. ml-metadata publishes no wheels for this platform, so "
            "a local CMF/MLMD runtime cannot exist here. Use server mode "
            "(provenance.artifacts.cmf.server_url), which needs no local CMF."
        ),
    },
    "cmf_local_runtime_unavailable": {
        "category": "runtime_configuration",
        "writes": False,
        "recovery_actions": ["configure_local_python", "configure_server_url"],
        "description": (
            "provenance.artifacts.cmf.python did not resolve to a usable local "
            "interpreter. The setting names a LOCAL interpreter path only: a "
            "multi-token launcher command (ssh/docker/srun ...) is not supported "
            "product surface, because CLIO cannot assume shell reach into "
            "another host. Point it at a local interpreter, or use server mode."
        ),
    },
    "cmf_server_unreachable": {
        "category": "downstream_unavailable",
        "writes": False,
        "recovery_actions": ["retry", "check_server_url", "check_network"],
        "description": (
            "The configured CMF server did not answer the metadata push within "
            "provenance.artifacts.cmf.publish_timeout_s (connection refused, DNS "
            "failure, TLS failure or timeout). Metadata for this batch was not "
            "recorded."
        ),
    },
    "cmf_server_rejected_payload": {
        "category": "downstream_contract",
        "writes": False,
        "recovery_actions": ["inspect_server_logs", "report_defect"],
        "description": (
            "The CMF server answered the metadata push with a refusal: a non-200 "
            "status, or a 200 whose body did not report success/exists. The "
            "synthesized document was not ingested."
        ),
    },
    "cmf_server_version_incompatible": {
        "category": "downstream_contract",
        "writes": False,
        "recovery_actions": ["upgrade_cmf_server", "report_defect"],
        "description": (
            "The CMF server answered 422/version_update, which its federation "
            "layer returns when an execution in the document carries no "
            "Execution_uuid property. CLIO always stamps one, so this states a "
            "genuine contract mismatch with the server's cmflib version rather "
            "than a malformed CLIO payload."
        ),
    },
    "cmf_artifact_not_attached_to_execution": {
        "category": "representation_limit",
        "writes": False,
        "recovery_actions": ["record_producing_transform", "report_defect"],
        "description": (
            "CMF's push document can only carry an artifact nested inside an "
            "execution's event (``execution.events[].artifact``); there is no "
            "free-standing artifact list. An artifact with no producing "
            "execution and no producer call_id to synthesize one from would be "
            "silently absent from the pushed document, so it is refused instead."
        ),
    },
    "cmf_artifact_reference_unresolved": {
        "category": "representation_limit",
        "writes": False,
        "recovery_actions": ["report_defect", "retry"],
        "description": (
            "A transform names an artifact id as a used/generated edge, but no "
            "artifact record is known for it -- not in the current push batch and "
            "not in the provider's record of earlier ones. The edge cannot be "
            "written into CMF's document (an artifact exists there only inside an "
            "event), and dropping it would silently cost the lineage graph an "
            "input, so the push is refused instead."
        ),
    },
    "cmf_artifact_kind_not_representable": {
        "category": "representation_limit",
        "writes": False,
        "recovery_actions": ["report_defect"],
        "description": (
            "A document reached the wire carrying a CMF artifact TYPE outside "
            "{Dataset, Model}, which the server drops in handle_event's "
            "'else: pass' while still answering success. Note this is about the "
            "storage class, never about CLIO's artifact kind: kind is an "
            "ontology (dataset, source, environment, report, plan, ...) that is "
            "preserved verbatim in clio_kind and narrowed to a storage class, so "
            "every artifact stays trackable. Reaching this reason means the "
            "narrowing was bypassed -- a CLIO defect, not a configuration issue."
        ),
    },
    "cmf_server_discarded_entities": {
        "category": "downstream_data_loss",
        "writes": False,
        "recovery_actions": ["retry", "inspect_server_logs", "report_upstream_defect"],
        "description": (
            "The CMF server answered 200 success but a bounded read-back shows "
            "it does not hold the executions that were just pushed. The known "
            "cause is an upstream defect: an execution whose properties contain "
            "a literal backslash is discarded WHOLE (with all its events) while "
            "the push still reports success -- cmf_merger.handle_execution wraps "
            "the write in 'except Exception: logger.error(...)', so nothing "
            "reaches the wire. CLIO encodes values to avoid it; reaching this "
            "reason means entities were lost anyway and must not be counted as "
            "written."
        ),
    },
    "cmf_lineage_query_unavailable": {
        "category": "capability_gap",
        "writes": False,
        "recovery_actions": ["configure_server_url", "configure_local_python"],
        "description": (
            "No CMF reader is available to answer a lineage query: server mode "
            "has no reachable server REST surface and no local MLMD store is "
            "configured to read instead."
        ),
    },
    # RESERVED, declared but never raised today. Shape (d) -- an in-stack CMF
    # write service CLIO posts to instead of running a worker -- introduces
    # provenance.artifacts.cmf.worker_url. The reason is declared now so the
    # catalog is the complete vocabulary of this seam from the start; the
    # selection code refuses the key as unknown configuration until that lands.
    "cmf_worker_url_unsupported": {
        "category": "capability_gap",
        "writes": False,
        "recovery_actions": ["configure_server_url"],
        "description": (
            "provenance.artifacts.cmf.worker_url names the in-stack CMF write "
            "service, which is not implemented yet. Reserved vocabulary: use "
            "server mode (server_url) until that deployment shape ships."
        ),
    },
}


def cmf_refusal_payload(reason: str, message: str = "", **details: Any) -> dict[str, Any]:
    """Build a structured, typed payload for one CMF refusal.

    Mirrors ``_stream_fallback_payload`` (validate against a typed catalog,
    reject unknowns) so a CMF degradation records a queryable reason instead of
    a bare failure or, worse, a silently dropped entity.

    Args:
        reason: A key of :data:`CMF_REFUSAL_REASON_DEFINITIONS`.
        message: Optional call-site detail naming the concrete value involved.
        **details: Optional structured context (paths, status codes, kinds).

    Returns:
        The reason, its catalog definition, and any supplied message/details.

    Raises:
        ValueError: ``reason`` is not in the closed catalog.
    """
    definition = CMF_REFUSAL_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown CMF refusal reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    if details:
        payload["details"] = dict(details)
    return payload


class CMFRefusal(RuntimeError):
    """A CMF write or query refused with a typed reason from the closed catalog.

    Carries the full :func:`cmf_refusal_payload` so every surface that sees the
    exception -- the dispatcher's ``ProviderHealth.last_error``, the lineage
    route's error envelope -- reports the same typed reason rather than a
    stringified traceback.
    """

    def __init__(self, reason: str, message: str = "", **details: Any) -> None:
        self.payload = cmf_refusal_payload(reason, message, **details)
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


def cmf_refusal_reasons() -> dict[str, dict[str, Any]]:
    """Project the catalog for read-only surfaces (health, diagnostics)."""
    return {
        reason: {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in definition.items()
        }
        for reason, definition in CMF_REFUSAL_REASON_DEFINITIONS.items()
    }


__all__ = [
    "CMF_REFUSAL_REASON_DEFINITIONS",
    "CMFRefusal",
    "cmf_refusal_payload",
    "cmf_refusal_reasons",
]
