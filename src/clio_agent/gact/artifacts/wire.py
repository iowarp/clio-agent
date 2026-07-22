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
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.records import ArtifactKind, ArtifactVersion
from clio_agent.gact.types import Part

if TYPE_CHECKING:
    from fastapi import FastAPI

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


__all__ = [
    "ARTIFACT_SERVER_ID",
    "PROPOSED_ARTIFACT_EVENT",
    "UI_PAYLOAD_MIME",
    "append_turn_resource_links",
    "artifact_uri",
    "fetch_url_for",
    "mime_for",
    "proposed_diff_payload",
    "resource_link_metadata",
    "resource_link_part",
    "ui_payload_uri",
]
