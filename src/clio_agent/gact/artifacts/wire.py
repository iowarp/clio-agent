"""Outbound wire identity for artifacts — the ``resource_link`` part projection.

Owner decision #966.9: an artifact gains outbound wire identity by reusing the
SPEC ``resource_link`` core part type (a client already MUST handle it; there
were zero emit sites before #968). At turn finalize one ``resource_link`` part is
emitted per artifact generated that turn — this is what finally gives a plot PNG
(or any deliverable) a wire reference instead of a path string the client must
guess. ``ui_payload`` artifacts (mcpui/a2ui) ride the SAME part with a ``ui://``
URI and the mcp-app HTML mime — record + delivery only; rendering is a later
campaign.

Pure projection: no app state, no I/O. It reads an :class:`ArtifactVersion` and
the logical ``(workspace_id, name)`` identity and returns a
:class:`~clio_agent.gact.types.Part`. The ``fetch_url`` points at the S2 bytes
route so a client can retrieve the content hash-verified.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.records import ArtifactKind, ArtifactVersion
from clio_agent.gact.types import Part
from clio_agent.runtime.humanize import format_bytes

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.proposals import ProposalOutcome

logger = logging.getLogger(__name__)

#: The ``artifact.proposed`` semantic-event type (the proposal stage of a
#: file-diff artifact). Trace-visible but deliberately OFF the SSE UI wire — its
#: payload is byte-parity-pinned by :func:`proposed_diff_payload` so a later
#: widening of the ``artifact.*`` family never drifts the proposal shape.
PROPOSED_ARTIFACT_EVENT = "artifact.proposed"


def proposed_diff_payload(
    path: str,
    unified_diff: str,
    new_content: str,
    edit_mode: str,
    lines_added: int,
    lines_removed: int,
) -> dict[str, Any]:
    """The ``artifact.proposed`` emit payload — the file_diff proposal fields (§7.3a).

    The single source of the proposal-event payload shape, called from
    ``turn_finalize`` so the emitted keys are exactly this set and a test can pin
    the real projection (not a source-text grep). Adding or removing a key here is
    a wire-shape change — the byte-parity lock reddens.
    """
    return {
        "path": path,
        "unified_diff": unified_diff,
        "new_content": new_content,
        "edit_mode": edit_mode,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
    }


#: The sentinel ``server_id`` an artifact ``resource_link`` carries so a client
#: distinguishes an artifact reference from an MCP-server resource reference
#: (which names the originating MCP server). Not an MCP server — a clio-internal
#: source served by the ``/v1/artifacts`` routes.
ARTIFACT_SERVER_ID = "clio-artifacts"

#: The mime an ``ui_payload`` artifact advertises (mcpui/a2ui HTML profile).
UI_PAYLOAD_MIME = "text/html;profile=mcp-app"

#: Suffix → mime for the common designated-output kinds. Deliberately small — a
#: best-effort content-type hint for the client, never load-bearing (the bytes
#: route serves the real content type). Unknown suffixes fall back per kind.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".parquet": "application/vnd.apache.parquet",
    ".h5": "application/x-hdf5",
    ".hdf5": "application/x-hdf5",
    ".nc": "application/x-netcdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".py": "text/x-python",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
}

#: Fallback mime per kind when the path suffix is unrecognised.
_MIME_BY_KIND: dict[ArtifactKind, str] = {
    ArtifactKind.IMAGE: "image/png",
    ArtifactKind.DATASET: "application/octet-stream",
    ArtifactKind.REPORT: "text/markdown",
    ArtifactKind.SCRIPT: "text/plain",
    ArtifactKind.CONFIG: "text/plain",
    ArtifactKind.MODEL: "application/octet-stream",
    ArtifactKind.UI_PAYLOAD: UI_PAYLOAD_MIME,
    ArtifactKind.OTHER: "application/octet-stream",
}


def artifact_uri(workspace_id: str, name: str, version: int) -> str:
    """The stable logical URI for an artifact version: ``artifact://<ws>/<name>@vN``."""
    return f"artifact://{workspace_id}/{name}@v{version}"


def ui_payload_uri(workspace_id: str, name: str, version: int) -> str:
    """The ``ui://<ws>/<name>@vN`` URI for a ``ui_payload`` artifact version."""
    return f"ui://{workspace_id}/{name}@v{version}"


def fetch_url_for(artifact_id: str) -> str:
    """The S2 bytes route a client GETs to retrieve the (hash-verified) content."""
    return f"/v1/artifacts/{artifact_id}/bytes"


def mime_for(version: ArtifactVersion, name: str) -> str:
    """Best-effort content-type hint for a version (suffix first, then kind)."""
    if version.kind == ArtifactKind.UI_PAYLOAD:
        return UI_PAYLOAD_MIME
    lowered = name.lower()
    dot = lowered.rfind(".")
    if dot != -1:
        suffix = lowered[dot:]
        if suffix in _MIME_BY_SUFFIX:
            return _MIME_BY_SUFFIX[suffix]
    return _MIME_BY_KIND.get(version.kind, "application/octet-stream")


def resource_link_metadata(
    workspace_id: str, name: str, version: ArtifactVersion
) -> dict[str, Any]:
    """The identity/provenance metadata block a ``resource_link`` part carries.

    The nine keys owner decision #966.9 pins —
    ``{artifact_id, sha256, size_bytes, kind, version, custody, fetch_url,
    producer_activity_id, mechanism}`` — PLUS the logical identity pair
    ``{workspace_id, name}`` the part's ``uri``/``name`` already encode but which a
    client keying off ``metadata`` alone would otherwise have to re-parse. Exactly
    these eleven keys (SPEC §4.5 documents all eleven; the test locks the set with
    equality, not a superset). ``producer_activity_id`` is the producing ``call_id``
    (the S5 TransformRecord key) or empty for non-tool mints.
    """
    return {
        "artifact_id": version.artifact_id,
        "sha256": version.sha256,
        "size_bytes": version.size_bytes,
        "kind": version.kind.value,
        "version": version.version,
        "custody": version.custody.value,
        "fetch_url": fetch_url_for(version.artifact_id),
        "producer_activity_id": str(version.producer.get("call_id") or ""),
        "mechanism": version.mechanism.value,
        "workspace_id": workspace_id,
        "name": name,
    }


def resource_link_part(
    workspace_id: str, name: str, version: ArtifactVersion, *, part_id: str, agent_id: str = ""
) -> Part:
    """Project one artifact version to a ``resource_link`` :class:`Part`.

    A ``ui_payload`` artifact rides the same part shape with a ``ui://`` URI and
    the mcp-app HTML mime (owner decision #966.9 — record + delivery only). All
    other kinds get an ``artifact://`` URI and a best-effort content mime.
    """
    is_ui = version.kind == ArtifactKind.UI_PAYLOAD
    uri = (
        ui_payload_uri(workspace_id, name, version.version)
        if is_ui
        else artifact_uri(workspace_id, name, version.version)
    )
    return Part(
        id=part_id,
        type="resource_link",
        agent_id=agent_id,
        server_id=ARTIFACT_SERVER_ID,
        uri=uri,
        name=name,
        mime_type=mime_for(version, name),
        metadata=resource_link_metadata(workspace_id, name, version),
    )


def append_turn_resource_links(
    app: "FastAPI", sid: str, turn_id: str, transcript: Any, *, agent_id: str = ""
) -> None:
    """Append one ``resource_link`` part per artifact generated this turn (#968 item 2).

    Drains the mint funnel's turn buffer (filtered to this turn — a leaked entry
    from a prior turn is popped with the list, never rides this message), projects
    each new version to a ``resource_link`` Part, and appends it to the transcript
    ledger so it persists + streams as a batch part. Owns the whole finalize-wire
    seam so ``turn_finalize`` stays a one-line caller (no-accretion). Fully guarded
    — a wire-identity append must never break the turn's answer.
    """
    try:
        from clio_agent.gact.artifacts.minting import drain_turn_artifacts  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _new_part_id  # noqa: PLC0415

        for entry in drain_turn_artifacts(app, sid, turn_id):
            version = entry.get("version")
            if version is None:
                continue
            transcript.append_part(
                resource_link_part(
                    str(entry.get("workspace_id") or ""),
                    str(entry.get("name") or ""),
                    version,
                    part_id=_new_part_id(),
                    agent_id=agent_id,
                ),
                stream_source="batch",
            )
    except Exception:  # noqa: BLE001 — a wire-identity append must never break a turn
        logger.warning(
            "artifact resource_link append skipped reason=resource_link_seam_failed session=%s",
            sid,
        )


def append_turn_child_resource_links(
    app: "FastAPI", sid: str, turn_id: str, transcript: Any, *, agent_id: str = ""
) -> None:
    """Roll up artifacts minted by CHILD sessions spawned THIS turn (owner ask
    2026-08-06): "show at the end of the turn ALL artifacts that have been
    generated in that turn by any of the agents or subagents".

    :func:`append_turn_resource_links` gives the PARENT's own mints wire identity
    from its per-session turn buffer; a delegated child's mints only ever chipped
    into the CHILD's own transcript because that buffer is session-scoped and
    already drained by the child's OWN finalize by the time this parent finalize
    runs — so the buffer is not a source here. Instead this walks the agent-task
    registry for the child sessions this parent TURN spawned (``parent_turn_id ==
    turn_id`` — a prior turn's child never matches) plus their full descendant
    tree (a child's own children, still entirely owned by this turn's work: each
    spawn mints a brand-new session, never reused across turns or parents), then
    reads every version those sessions produced from the artifact REGISTRY — the
    authoritative in-process projection (RULE 4), not a heuristic.

    Appended as one contiguous run, ordered by mint time, AFTER
    :func:`append_turn_resource_links` so both runs land adjacent on the message
    (one ARTIFACTS grid client-side); any ``artifact_id`` already riding a
    ``resource_link`` part on this message is skipped, so the parent's own chips
    can never duplicate. No descendant spawned this turn -> no registry scan, no
    parts (never an empty grid marker). Fully guarded: a rollup failure must
    never break the turn's answer.
    """
    try:
        reg = getattr(app.state, "agent_task_registry", None)
        if reg is None:
            return
        direct_children = [
            str(task.child_session_id)
            for task in reg.for_parent(sid)
            if task.child_session_id and task.parent_turn_id == turn_id
        ]
        if not direct_children:
            return

        from clio_agent.gact.agent_tasks import descendant_session_ids  # noqa: PLC0415
        from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import _new_part_id  # noqa: PLC0415

        session_ids: set[str] = set(direct_children)
        for child_sid in direct_children:
            session_ids.update(descendant_session_ids(app, child_sid))

        already = {
            str(part.metadata.get("artifact_id") or "")
            for part in transcript.snapshot()
            if part.type == "resource_link"
        }

        rows: list[tuple[str, str, ArtifactVersion]] = []
        for record in get_registry(app).all_records():
            for version in record.versions:
                producer_sid = str((version.producer or {}).get("session_id") or "")
                if producer_sid not in session_ids or version.artifact_id in already:
                    continue
                rows.append((record.workspace_id, record.name, version))
        # Several parallel children can prepare the same logical artifact (for
        # example, a shared station catalog).  Preserve every distinct artifact,
        # but roll up only the newest version of a repeated ``workspace/name``
        # identity.  Version history remains available through the registry; the
        # parent conversation should not receive a stack of obsolete copies.
        latest_by_logical_name: dict[tuple[str, str], tuple[str, str, ArtifactVersion]] = {}
        for row in rows:
            key = (row[0], row[1])
            current = latest_by_logical_name.get(key)
            if current is None or row[2].version > current[2].version:
                latest_by_logical_name[key] = row
        rows = list(latest_by_logical_name.values())
        rows.sort(key=lambda row: row[2].created_at or "")

        for workspace_id, name, version in rows:
            transcript.append_part(
                resource_link_part(
                    workspace_id, name, version, part_id=_new_part_id(), agent_id=agent_id
                ),
                stream_source="batch",
            )
    except Exception:  # noqa: BLE001 — a wire-identity rollup must never break a turn
        logger.warning(
            "artifact child resource_link rollup skipped reason=turn_rollup_failed session=%s",
            sid,
        )


def create_artifact_summary_message(outcomes: "Sequence[ProposalOutcome]") -> str:
    """One-line wire summary for ``create_artifact``'s declared ``structured_content``
    (P5 wire semantics — the ``wait_agent_tasks`` treatment): derived directly from
    each outcome's own accepted/created/reason facts — never invented, never a
    second guess at what ``promote_proposals`` already decided.

    A single-item batch (the common case) names the ONE outcome: a fresh mint with
    its size, a dedup against the existing version, or the typed rejection reason.
    A multi-item batch reports the created/deduplicated/rejected tally plus a
    bounded sample of the first few names (never a raw dump).
    """

    if len(outcomes) == 1:
        outcome = outcomes[0]
        if not outcome.accepted:
            return f"rejected: {outcome.reason}"
        if outcome.created:
            size = outcome.version.size_bytes if outcome.version is not None else None
            suffix = f" ({format_bytes(size)})" if isinstance(size, int) else ""
            return f"created 1 artifact: {outcome.name}{suffix}"
        version_n = outcome.version.version if outcome.version is not None else 0
        return f"deduplicated against existing {outcome.name} v{version_n}"
    created = sum(1 for o in outcomes if o.accepted and o.created)
    deduplicated = sum(1 for o in outcomes if o.accepted and not o.created)
    rejected = sum(1 for o in outcomes if not o.accepted)
    names = [o.name for o in outcomes if o.name]
    sample = ", ".join(names[:3])
    if len(names) > 3:
        sample += f", +{len(names) - 3} more"
    summary = (
        f"{len(outcomes)} artifacts: {created} created, "
        f"{deduplicated} deduplicated, {rejected} rejected"
    )
    return f"{summary} ({sample})" if sample else summary


def declare_create_artifact_structured_content(
    outcomes: "Sequence[ProposalOutcome]", result: Mapping[str, Any]
) -> None:
    """Declare ``create_artifact``'s typed wire payload: the composed summary
    ``message`` FIRST, then the SAME ``artifacts``/``created``/``deduplicated``/
    ``rejected`` fields the model-facing ``result`` already carries. Both of
    ``promote_proposals``'s return points stay a single call — the substantive
    logic lives HERE so proposals.py's ratcheted line count stays flat.
    """

    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    declare_structured_content({"message": create_artifact_summary_message(outcomes), **result})


__all__ = [
    "ARTIFACT_SERVER_ID",
    "PROPOSED_ARTIFACT_EVENT",
    "UI_PAYLOAD_MIME",
    "append_turn_child_resource_links",
    "append_turn_resource_links",
    "artifact_uri",
    "create_artifact_summary_message",
    "declare_create_artifact_structured_content",
    "fetch_url_for",
    "mime_for",
    "proposed_diff_payload",
    "resource_link_metadata",
    "resource_link_part",
    "ui_payload_uri",
]
