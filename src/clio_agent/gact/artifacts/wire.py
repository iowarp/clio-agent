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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from clio_agent.gact.artifacts.records import ArtifactKind, ArtifactVersion
from clio_agent.gact.types import Part
from clio_agent.runtime.humanize import format_bytes
from clio_agent.tools.mcp_extension_registry import MCP_APP_MIME_TYPE as UI_PAYLOAD_MIME

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.proposals import ProposalOutcome

logger = logging.getLogger(__name__)

#: The sentinel ``server_id`` an artifact ``resource_link`` carries so a client
#: distinguishes an artifact reference from an MCP-server resource reference
#: (which names the originating MCP server). Not an MCP server — a clio-internal
#: source served by the ``/v1/artifacts`` routes.
ARTIFACT_SERVER_ID = "clio-artifacts"

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


def version_uri(workspace_id: str, name: str, version: ArtifactVersion) -> str:
    """Return the logical URI using the immutable version's artifact kind."""
    if version.kind == ArtifactKind.UI_PAYLOAD:
        return ui_payload_uri(workspace_id, name, version.version)
    return artifact_uri(workspace_id, name, version.version)


def fetch_url_for(artifact_id: str) -> str:
    """The S2 bytes route a client GETs to retrieve the (hash-verified) content."""
    return f"/v1/artifacts/{artifact_id}/bytes"


def workspace_file_fetch_url(workspace_id: str, source: Path, root: Path) -> str:
    """The workspace file route that serves a workspace-referenced artifact's bytes.

    Both the workspace id and the workspace-relative path are percent-encoded, so
    a file name carrying ``&``, ``#``, ``+`` or ``%`` survives as ONE ``path``
    query value instead of being split, truncated at a fragment, or decoded into
    a different name. A source that is not under ``root`` yields an empty path
    (the route then answers with its own typed refusal).
    """
    try:
        rel = source.expanduser().resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        rel = ""
    query = urlencode({"path": rel}, quote_via=quote)
    return f"/v1/workspaces/{quote(workspace_id, safe='')}/files/read?{query}"


def mime_for(version: ArtifactVersion, name: str) -> str:
    """Prefer a published filename's type, then the source for friendly labels."""
    if version.kind == ArtifactKind.UI_PAYLOAD:
        return UI_PAYLOAD_MIME
    from clio_agent.gact.documents.profiles import document_format, supported_extensions

    for candidate in (name, version.path):
        suffix = Path(candidate).suffix.lower()
        if suffix in _MIME_BY_SUFFIX:
            return _MIME_BY_SUFFIX[suffix]
        if suffix in supported_extensions():
            return document_format(candidate).mime_type
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
    """Append the latest deliverable version per family selected this turn.

    Drains the mint funnel's turn buffer (filtered to this turn — a leaked entry
    from a prior turn is popped with the list, never rides this message), projects
    eligible output versions to ``resource_link`` Parts, and appends them to the transcript
    ledger so it persists + streams as a batch part. Owns the whole finalize-wire
    seam so ``turn_finalize`` stays a one-line caller (no-accretion). Fully guarded
    — a wire-identity append must never break the turn's answer.
    """
    try:
        from clio_agent.gact.artifacts.minting import drain_turn_artifacts  # noqa: PLC0415
        from clio_agent.gact.artifacts.presentation import response_deliverables
        from clio_agent.gact.runtime.globals import _new_part_id  # noqa: PLC0415

        for entry in response_deliverables(drain_turn_artifacts(app, sid, turn_id)):
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
    """Project deliverables from authorized delegations originating in this turn.

    Core supplies immutable assignment lineage. This only appends resource links;
    it neither settles children nor reconstructs their execution history.
    """
    from clio_agent.arc.task_types import AssignmentDetails
    from clio_agent.gact.artifacts.presentation import child_response_owners, response_deliverables
    from clio_agent.gact.artifacts.registry import get_registry
    from clio_agent.gact.runtime.globals import _new_part_id
    from clio_agent.gact.task_access import TaskAccess

    history = app.state.task_history
    access = TaskAccess(history, app.state.conversations.caller(sid))
    sessions: set[str] = set()
    for handle in history.tree_handles(sid):
        child = access.require(handle, "read")
        parent = (
            history.task_snapshot(child.owner.parent_handle) if child.owner.parent_handle else None
        )
        if (
            child.kind != "Subagent"
            or parent is None
            or parent.owner.session_id != sid
            or child.details is None
        ):
            continue
        details = AssignmentDetails.model_validate(history.read_content(child.details))
        if details.parent_turn_id != turn_id:
            continue
        for descendant in history.tree_handles(child.owner.session_id):
            task = access.require(descendant, "read")
            if task.kind in {"Session", "Subagent"}:
                sessions.add(task.owner.session_id)
    if not sessions:
        return
    already = {
        str(part.metadata.get("artifact_id") or "")
        for part in transcript.snapshot()
        if part.type == "resource_link"
    }
    published = child_response_owners(app, sessions)
    entries: list[dict[str, Any]] = [
        {
            "workspace_id": record.workspace_id,
            "name": record.name,
            "version": version,
            "purpose": "deliverable" if version.artifact_id in published else None,
            "response_session_id": published.get(version.artifact_id),
        }
        for record in get_registry(app).all_records()
        for version in record.versions
        if (
            str(version.producer.get("session_id") or "") in sessions
            or version.artifact_id in published
        )
        and version.artifact_id not in already
    ]
    entries.sort(key=lambda entry: entry["version"].created_at or "")
    for entry in response_deliverables(entries):
        transcript.append_part(
            resource_link_part(
                entry["workspace_id"],
                entry["name"],
                entry["version"],
                part_id=_new_part_id(),
                agent_id=agent_id,
            ),
            stream_source="batch",
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
    "UI_PAYLOAD_MIME",
    "append_turn_child_resource_links",
    "append_turn_resource_links",
    "artifact_uri",
    "create_artifact_summary_message",
    "declare_create_artifact_structured_content",
    "fetch_url_for",
    "mime_for",
    "resource_link_metadata",
    "resource_link_part",
    "ui_payload_uri",
    "workspace_file_fetch_url",
]
