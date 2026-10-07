"""Generated transform edges, including separately owned document previews."""

from clio_agent.gact.artifacts.records import ArtifactVersion
from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge


def generated_edges(
    minted: list[ArtifactVersion], *, call_id: str = "", fence_proven: bool = False
) -> list[ProvEdge]:
    """Project the versions minted this call to ``generated`` edges.

    ``fence_proven`` (B6 #980) stamps the per-edge lease-window → fence_proven upgrade on
    every generated edge when an active OS fence proved this call's output territory exclusive
    by construction (``transform_exclusivity.generated_fence_proven``). Identity evidence
    (``hash-pair`` / ``schema-arg``) is unchanged — the marker is a separate attribution axis.

    A version whose recorded producing ``call_id`` is a DIFFERENT call than ``call_id``
    was not appended by this call: the mint deduped this call's byte-identical output
    onto an existing version (W&B same-sha dedup, owner decision #966.3 — the dedup
    no-op deliberately emits no artifact event). The edge is still true provenance —
    this call really re-wrote the bytes — but it carries ``note="same_sha_dedup"`` so
    the trace distinguishes a re-production from a fresh mint (no-silent-fallback:
    without the note, a deduped re-run is indistinguishable from a v1 mint on the
    trace, the exact ambiguity behind the 2026-08-05 ndp re-run investigation).
    Versions with no recorded producing call (reconcile / pack / harness producers)
    are never stamped — precision over recall (#966.10).
    """
    edges: list[ProvEdge] = []
    for version in minted:
        producing_call = str((version.producer or {}).get("call_id") or "")
        # A nested preview conversion has its own source-bound transform.
        if (
            version.producer.get("designation") == "document-rendition"
            and producing_call != call_id
        ):
            continue
        deduped = bool(call_id) and bool(producing_call) and producing_call != call_id
        note = "" if version.sha256 else "stat_pinned"
        if deduped:
            note = "same_sha_dedup"
        edges.append(
            ProvEdge(
                role=EdgeRole.GENERATED,
                evidence=(EdgeEvidence.HASH_PAIR if version.sha256 else EdgeEvidence.SCHEMA_ARG),
                artifact_id=version.artifact_id,
                sha256=version.sha256,
                name="",
                version=version.version,
                path=version.path,
                note=note,
                fence_proven=fence_proven,
            )
        )
    return edges
