"""The ONE grammar for CLIO content references a viewer resolves (A2 remote media).

A2UI media and artifact components carry a *reference*, not a fetchable URL:
the viewer may sit on another machine than the service (a desktop or web client
connected to a remote CLIO), so the browser cannot dereference ``artifact:`` or
``resource:`` itself, and it must present the service's bearer to read bytes.
Clients therefore ask the service to resolve a reference into metadata plus the
service-relative ``fetch_path`` of the EXISTING byte route (``/v1/artifacts/{id}/
bytes`` or ``/v1/workspaces/{ws}/resources/{id}/content``), then read those bytes
through their authenticated transport. There is no second byte-serving path: the
artifact serve ladder (hash verification, custody redirect) stays the only one.

Accepted forms (the same set the A2UI validator admits):

* ``artifact://artifact_<id>``, ``artifact:artifact_<id>``, bare ``artifact_<id>``
  -- one immutable version;
* ``artifact://<workspace>/<name>@<ref>`` -- the logical version URI the
  ``resource_link`` part carries (``ref`` is ``vN``, ``latest`` or an alias; a
  missing ``@ref`` means ``latest``). Names are matched verbatim first, then
  percent-decoded, so a client that URL-encoded the name still resolves;
* ``resource://<workspace>/res_<id>``, ``resource://res_<id>``, ``resource:res_<id>``,
  bare ``res_<id>`` -- an uploaded workspace resource; the workspace-less forms
  resolve in the requesting session's workspace.

Every failure is a typed :class:`ReferenceResolutionError` carrying the HTTP status, a
stable ``error`` code and details -- never a guess or a substituted payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional
from urllib.parse import quote, unquote

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion

#: A bare artifact version id, as minted by the registry (``artifact_<hex>``).
ARTIFACT_ID_PATTERN = re.compile(r"^artifact_[A-Za-z0-9]+$")
#: A bare workspace resource id, as minted by the resource store (``res_<hex>``).
RESOURCE_ID_PATTERN = re.compile(r"^res_[A-Za-z0-9]+$")


def is_bare_reference_id(value: str) -> bool:
    """Whether ``value`` is a bare CLIO artifact or resource id (no scheme)."""

    return bool(ARTIFACT_ID_PATTERN.match(value) or RESOURCE_ID_PATTERN.match(value))


class ReferenceResolutionError(Exception):
    """A typed resolution failure: HTTP status + stable code + details."""

    def __init__(self, status_code: int, error: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ParsedReference:
    """One parsed reference. ``kind`` selects which fields are meaningful."""

    kind: Literal["artifact_id", "artifact_name", "resource"]
    artifact_id: str = ""
    workspace_id: str = ""
    name: str = ""
    ref: str = "latest"
    resource_id: str = ""


def parse_reference(uri: str) -> ParsedReference:
    """Parse one reference string into its form, or raise ``reference_uri_invalid``."""

    value = uri.strip()
    if ARTIFACT_ID_PATTERN.match(value):
        return ParsedReference(kind="artifact_id", artifact_id=value)
    if RESOURCE_ID_PATTERN.match(value):
        return ParsedReference(kind="resource", resource_id=value)
    scheme, sep, rest = value.partition(":")
    scheme = scheme.lower()
    if sep and scheme == "artifact":
        body = rest[2:] if rest.startswith("//") else rest
        if ARTIFACT_ID_PATTERN.match(body):
            return ParsedReference(kind="artifact_id", artifact_id=body)
        workspace_id, slash, name_ref = body.partition("/")
        if slash and workspace_id and name_ref:
            name, at, ref = name_ref.rpartition("@")
            if at and name and ref:
                return ParsedReference(
                    kind="artifact_name", workspace_id=workspace_id, name=name, ref=ref
                )
            return ParsedReference(kind="artifact_name", workspace_id=workspace_id, name=name_ref)
    if sep and scheme == "resource":
        body = rest[2:] if rest.startswith("//") else rest
        workspace_id, slash, resource_id = body.rpartition("/")
        if RESOURCE_ID_PATTERN.match(resource_id) and (workspace_id or not slash):
            return ParsedReference(
                kind="resource", workspace_id=workspace_id, resource_id=resource_id
            )
    raise ReferenceResolutionError(
        422,
        "reference_uri_invalid",
        "not a CLIO artifact or resource reference",
        uri=uri,
        accepted=[
            "artifact://artifact_<id>",
            "artifact:artifact_<id>",
            "artifact://<workspace>/<name>@<ref>",
            "resource://<workspace>/res_<id>",
            "resource://res_<id>",
            "resource:res_<id>",
            "artifact_<id>",
            "res_<id>",
        ],
    )


def _artifact_resolution(
    uri: str, record: "ArtifactRecord", version: "ArtifactVersion"
) -> dict[str, Any]:
    from clio_agent.gact.artifacts.wire import fetch_url_for, mime_for  # noqa: PLC0415

    return {
        "uri": uri,
        "kind": "artifact",
        "workspace_id": record.workspace_id,
        "name": record.name,
        "media_type": mime_for(version, record.name),
        "size_bytes": version.size_bytes,
        "artifact_id": version.artifact_id,
        "version": version.version,
        "custody": version.custody.value,
        "fetch_path": fetch_url_for(quote(version.artifact_id, safe="")),
    }


def _resolve_named(registry: Any, parsed: ParsedReference, uri: str) -> dict[str, Any]:
    from clio_agent.gact.routes.artifacts import _available_refs, _resolve_ref  # noqa: PLC0415

    candidates = [(parsed.name, parsed.ref)]
    decoded = unquote(parsed.name)
    if decoded != parsed.name:
        candidates.append((decoded, unquote(parsed.ref)))
    if parsed.ref != "latest":
        # ``@`` is legal inside a file name: when ``<name>@<ref>`` names no record,
        # the whole tail may be the name itself (addressed at its latest version).
        full = f"{parsed.name}@{parsed.ref}"
        candidates.extend([(full, "latest"), (unquote(full), "latest")])
    for name, ref in candidates:
        record = registry.get(parsed.workspace_id, name)
        if record is None:
            continue
        version = _resolve_ref(record, ref)
        if version is None:
            raise ReferenceResolutionError(
                404,
                "reference_not_found",
                f"artifact ref not resolvable: {ref}",
                uri=uri,
                workspace_id=parsed.workspace_id,
                name=name,
                ref=ref,
                available=_available_refs(record),
            )
        return _artifact_resolution(uri, record, version)
    raise ReferenceResolutionError(
        404,
        "reference_not_found",
        f"artifact not found: {parsed.name}",
        uri=uri,
        workspace_id=parsed.workspace_id,
        name=parsed.name,
    )


def _resolve_resource(
    app: "FastAPI", parsed: ParsedReference, uri: str, session_workspace_id: Optional[str]
) -> dict[str, Any]:
    workspace_id = parsed.workspace_id or (session_workspace_id or "")
    if not workspace_id:
        raise ReferenceResolutionError(
            409,
            "reference_workspace_unresolved",
            "a workspace-less resource reference needs a session bound to a workspace",
            uri=uri,
            resource_id=parsed.resource_id,
        )
    store = getattr(app.state, "resource_store", None)
    record = store.get(workspace_id, parsed.resource_id) if store is not None else None
    if record is None:
        raise ReferenceResolutionError(
            404,
            "reference_not_found",
            f"resource not found: {parsed.resource_id}",
            uri=uri,
            workspace_id=workspace_id,
            resource_id=parsed.resource_id,
        )
    return {
        "uri": uri,
        "kind": "resource",
        "workspace_id": workspace_id,
        "name": record.name,
        "media_type": record.detected_mime or record.claimed_mime or "application/octet-stream",
        "size_bytes": record.received_size or record.declared_size,
        "resource_id": record.id,
        "state": record.state,
        "fetch_path": (
            f"/v1/workspaces/{quote(workspace_id, safe='')}"
            f"/resources/{quote(record.id, safe='')}/content"
        ),
    }


async def resolve_reference(
    app: "FastAPI", uri: str, *, session_workspace_id: Optional[str]
) -> dict[str, Any]:
    """Resolve ``uri`` to its metadata and the byte route to read it through.

    Args:
        app: The GACT app (registry + resource store live on ``app.state``).
        uri: The reference string exactly as the A2UI payload carried it.
        session_workspace_id: The requesting session's workspace, used only by
            the workspace-less ``resource`` forms.

    Returns:
        ``{uri, kind, workspace_id, name, media_type, size_bytes, fetch_path, ...}``.

    Raises:
        ReferenceResolutionError: ``reference_uri_invalid`` (422), ``reference_not_found``
            (404) or ``reference_workspace_unresolved`` (409).
    """

    parsed = parse_reference(uri)
    if parsed.kind == "resource":
        return _resolve_resource(app, parsed, uri, session_workspace_id)
    from clio_agent.gact.routes.artifacts import _registry  # noqa: PLC0415

    registry = await _registry(app)
    if parsed.kind == "artifact_id":
        found = registry.get_by_artifact_id(parsed.artifact_id)
        if found is None:
            raise ReferenceResolutionError(
                404,
                "reference_not_found",
                f"artifact not found: {parsed.artifact_id}",
                uri=uri,
                artifact_id=parsed.artifact_id,
            )
        record, version = found
        return _artifact_resolution(uri, record, version)
    return _resolve_named(registry, parsed, uri)


__all__ = [
    "ARTIFACT_ID_PATTERN",
    "RESOURCE_ID_PATTERN",
    "ParsedReference",
    "ReferenceResolutionError",
    "is_bare_reference_id",
    "parse_reference",
    "resolve_reference",
]
