"""Bounded transfer of one remote relay artifact into the session workspace.

The demo slice of #1200. Execution outputs are produced on a cluster and stay
there: relay indexes them by reference (identifier, digest, size) and clio-agent
cites them without moving bytes. This module is the one deliberate exception --
an explicit, size-checked, agent-requested transfer for the case where the bytes
must be analyzed locally.

Two rules give the surface its shape.

*The size check precedes the transfer.* Relay's own job artifact index carries
``size_bytes``, so an oversize artifact is refused from the LISTING and no
download is ever started. The refusal is typed and carries the size plus the
remote reference, so an agent can report exactly where the data lives. There is
no partial download and no truncation -- a scientific run can emit gigabytes and
an agent must never trigger that transfer by accident.

*The transfer is a change of custody, and is recorded as one.* The written file
is minted through the existing tool-result designation seam, and the remote
origin rides the result so
:func:`clio_agent.gact.artifacts.transform_edges.detect_authority_edges` can
attach the used-edge naming the cluster the bytes were produced on.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations

from clio_agent import conf
from clio_agent.tools.relay_transport import RelayTransportContractError

RELAY_FETCH_TOOL_NAME = "relay_fetch_artifact"
RELAY_FETCH_BARE_NAME = "fetch_artifact"
RELAY_FETCH_ORIGIN_SCHEMA = "clio-agent.relay-artifact-origin.v1"

# Generous by design: this is the ceiling that stops an accidental multi-gigabyte
# transfer, not a policy about what is worth fetching. Resolved file -> env ->
# default through the single config store (``clio_agent.conf``), the same way
# every other byte limit in the tool layer is resolved.
_DEFAULT_FETCH_MAX_BYTES = 100 * 1024 * 1024

# clio-relay's OWN ceiling on one artifact-content transfer
# (``MAX_ARTIFACT_CONTENT_BYTES`` in ``clio_relay/relay_ops.py``, enforced in
# ``read_artifact_bytes``): a larger request is refused server-side, never
# truncated. Mirrored here so the refusal arrives as a reason an agent can
# report rather than as an opaque HTTP failure part-way through a transfer.
RELAY_ARTIFACT_CONTENT_LIMIT_BYTES = 16 * 1024 * 1024


def fetch_max_bytes() -> int:
    """Resolve the maximum artifact size this deployment will transfer inline.

    Resolved per call rather than at import so a workspace config change takes
    effect without a restart. A non-positive or unparseable value is refused
    outright instead of being silently replaced -- an unbounded fetch is exactly
    the failure this knob exists to prevent.
    """

    try:
        resolved = conf.resolve(
            "relay.fetch_max_bytes",
            env="CLIO_RELAY_FETCH_MAX_BYTES",
            default=_DEFAULT_FETCH_MAX_BYTES,
            cast=conf.as_int,
        )
    except (TypeError, ValueError) as exc:
        raise RelayArtifactFetchError(
            "relay.fetch_max_bytes is not an integer",
            reason="relay_fetch_limit_invalid",
            details={"env": "CLIO_RELAY_FETCH_MAX_BYTES"},
        ) from exc
    if resolved <= 0:
        raise RelayArtifactFetchError(
            "relay.fetch_max_bytes must be positive",
            reason="relay_fetch_limit_invalid",
            details={"observed": resolved},
        )
    return resolved


class RelayArtifactFetchError(RelayTransportContractError):
    """A remote artifact transfer was refused or violated its contract."""


class RelayArtifactClient(Protocol):
    """The two relay operations one bounded artifact transfer needs."""

    async def list_job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        """Return relay's indexed artifact records for one job, sizes included."""
        ...

    async def fetch_artifact(self, artifact_id: str) -> bytes:
        """Return one relay artifact's decoded content bytes."""
        ...


RelayArtifactClientFactory = Callable[[], AbstractAsyncContextManager[RelayArtifactClient]]

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "The relay job that produced the artifact.",
        },
        "artifact_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "The relay artifact identifier to transfer.",
        },
        "target_filename": {
            "anyOf": [{"type": "string", "minLength": 1, "maxLength": 255}, {"type": "null"}],
            "default": None,
            "description": (
                "Optional file name to write, a bare name with no directory part. "
                "Defaults to the remote artifact's own file name."
            ),
        },
    },
    "required": ["job_id", "artifact_id"],
    "additionalProperties": False,
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "local_path": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "sha256": {"type": ["string", "null"]},
        "origin": {"type": "object"},
    },
    "required": ["local_path", "size_bytes", "sha256", "origin"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Use this when an execution's output file must be analyzed locally: it "
    "transfers one relay artifact's bytes into this session's workspace and "
    "returns the local path, size, and remote origin. The recorded size is "
    "checked first and an oversize artifact is refused with its size and "
    "reference rather than downloaded. Reading logs or stdout does not need "
    "this -- those come back inline from the observation tools. This never "
    "returns file content."
)


def _description(cluster_hint: str | None) -> str:
    """Compose the description, naming the deployment's cluster when configured.

    Unlike the relay follow tools, this tool takes no ``cluster`` argument at
    all -- a job_id plus artifact_id already names the origin -- so the sentence
    states a fact and can never steer a caller into supplying a route field.
    Unset leaves the description byte-identical.
    """

    if not cluster_hint:
        return _DESCRIPTION
    return f"{_DESCRIPTION} This deployment's registered cluster is {cluster_hint!r}."


def _target_name(target_filename: Any, record: Mapping[str, Any], artifact_id: str) -> str:
    """Resolve the bare file name to write, rejecting any directory component."""

    if target_filename is None:
        uri = record.get("uri")
        remote_name = Path(str(uri)).name if isinstance(uri, str) and uri else ""
        candidate = remote_name or f"{artifact_id}.bin"
    else:
        candidate = str(target_filename).strip()
    if not candidate or candidate in {".", ".."}:
        raise RelayArtifactFetchError(
            "target_filename must name a file",
            reason="relay_fetch_target_invalid",
            details={"target_filename": candidate},
        )
    if Path(candidate).name != candidate:
        raise RelayArtifactFetchError(
            "target_filename must be a bare file name with no directory part",
            reason="relay_fetch_target_invalid",
            details={"target_filename": candidate},
        )
    return candidate


def _origin(record: Mapping[str, Any], job_id: str, cluster: str | None) -> dict[str, Any]:
    """The remote reference this transfer took custody from."""

    return {
        "schema_version": RELAY_FETCH_ORIGIN_SCHEMA,
        "cluster": cluster or None,
        "job_id": job_id,
        "artifact_id": str(record.get("artifact_id") or ""),
        "uri": record.get("uri") if isinstance(record.get("uri"), str) else None,
        "kind": record.get("kind") if isinstance(record.get("kind"), str) else None,
        "remote_size_bytes": record.get("size_bytes"),
        "remote_sha256": record.get("sha256") if isinstance(record.get("sha256"), str) else None,
        "transferred_by": RELAY_FETCH_TOOL_NAME,
    }


def _select(records: list[dict[str, Any]], artifact_id: str, job_id: str) -> dict[str, Any]:
    """Find the requested artifact in relay's own index for that job."""

    for record in records:
        if str(record.get("artifact_id") or "") == artifact_id:
            return record
    raise RelayArtifactFetchError(
        f"relay job {job_id} does not index artifact {artifact_id}",
        reason="relay_fetch_artifact_not_indexed",
        details={
            "job_id": job_id,
            "artifact_id": artifact_id,
            "indexed_artifact_ids": [str(r.get("artifact_id") or "") for r in records][:50],
        },
    )


def _require_within_limit(record: Mapping[str, Any], origin: Mapping[str, Any]) -> int:
    """Refuse an oversize transfer from the LISTING, before any bytes move.

    Two ceilings apply and the LOWER one decides, because clearing this check
    only to be refused mid-transfer by relay would leave the agent holding an
    opaque HTTP failure instead of a reason it can report. Relay's artifact
    content endpoint enforces its own ``MAX_ARTIFACT_CONTENT_BYTES``
    (16 MiB, ``clio_relay/relay_ops.py``) and answers a larger request with a
    typed transfer-limit error, so this refuses at that boundary too and names
    which of the two limits bound the call.
    """

    size = record.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RelayArtifactFetchError(
            "relay artifact record carries no usable size; refusing an unbounded transfer",
            reason="relay_fetch_size_unknown",
            details={"origin": dict(origin), "observed_size_bytes": size},
        )
    configured = fetch_max_bytes()
    limit = min(configured, RELAY_ARTIFACT_CONTENT_LIMIT_BYTES)
    if size > limit:
        bound_by = (
            "relay.fetch_max_bytes" if configured <= RELAY_ARTIFACT_CONTENT_LIMIT_BYTES else "relay"
        )
        raise RelayArtifactFetchError(
            f"relay artifact is {size} bytes, above the {limit}-byte transfer limit; "
            "it was not downloaded and stays where it was produced",
            reason="relay_fetch_artifact_too_large",
            details={
                "size_bytes": size,
                "max_bytes": limit,
                "bound_by": bound_by,
                "configured_max_bytes": configured,
                "relay_max_bytes": RELAY_ARTIFACT_CONTENT_LIMIT_BYTES,
                "config_key": "relay.fetch_max_bytes",
                "env": "CLIO_RELAY_FETCH_MAX_BYTES",
                "origin": dict(origin),
            },
        )
    return size


def _verify(payload: bytes, expected_size: int, origin: Mapping[str, Any]) -> str:
    """Check the transferred bytes against relay's recorded identity."""

    digest = hashlib.sha256(payload).hexdigest()
    expected_sha = origin.get("remote_sha256")
    if len(payload) != expected_size:
        raise RelayArtifactFetchError(
            "relay artifact content length does not match its indexed size",
            reason="relay_fetch_size_mismatch",
            details={
                "expected_size_bytes": expected_size,
                "observed_size_bytes": len(payload),
                "origin": dict(origin),
            },
        )
    if isinstance(expected_sha, str) and expected_sha and expected_sha.lower() != digest:
        raise RelayArtifactFetchError(
            "relay artifact content does not match its indexed digest",
            reason="relay_fetch_digest_mismatch",
            details={
                "expected_sha256": expected_sha,
                "observed_sha256": digest,
                "origin": dict(origin),
            },
        )
    return digest


async def fetch_relay_artifact(
    client_factory: RelayArtifactClientFactory,
    arguments: Mapping[str, Any],
    *,
    cluster_hint: str | None = None,
) -> dict[str, Any]:
    """Transfer one relay artifact into the session workspace, size-checked first."""

    from clio_agent.tools.execution import get_active_tool_workspace_root  # noqa: PLC0415
    from clio_agent.tools.file_policy import validate_write_path  # noqa: PLC0415

    job_id = str(arguments.get("job_id") or "").strip()
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not job_id or not artifact_id:
        raise RelayArtifactFetchError(
            "relay artifact fetch needs both job_id and artifact_id",
            reason="relay_fetch_identity_missing",
            details={"job_id": job_id, "artifact_id": artifact_id},
        )

    async with client_factory() as relay:
        records = await relay.list_job_artifacts(job_id)
        record = _select(records, artifact_id, job_id)
        origin = _origin(record, job_id, cluster_hint)
        expected_size = _require_within_limit(record, origin)
        payload = await relay.fetch_artifact(artifact_id)

    digest = _verify(payload, expected_size, origin)
    name = _target_name(arguments.get("target_filename"), record, artifact_id)
    # The file lands in THIS session's workspace, never the process cwd: a
    # transferred output has to sit where the session's own outputs sit for the
    # artifact seams to see it at all. Off-turn there is no workspace to write
    # into, and that is a typed refusal rather than a write somewhere arbitrary.
    workspace_root = (get_active_tool_workspace_root() or "").strip()
    if not workspace_root:
        raise RelayArtifactFetchError(
            "no session workspace is bound; a fetched artifact has nowhere to land",
            reason="relay_fetch_workspace_unbound",
            details={"origin": origin},
        )
    destination = validate_write_path(
        str(Path(workspace_root) / name), field="target_filename", create_parent=True
    )
    destination.write_bytes(payload)

    return {
        "local_path": str(destination),
        "size_bytes": len(payload),
        "sha256": digest,
        "origin": origin,
    }


class RelayArtifactFetchTool(Tool):
    """The bounded artifact transfer exposed below the gateway's ``relay`` mount."""

    def __init__(
        self,
        *,
        client_factory: RelayArtifactClientFactory,
        cluster_hint: str | None = None,
    ) -> None:
        super().__init__(
            name=RELAY_FETCH_BARE_NAME,
            title="Fetch Artifact",
            description=_description(cluster_hint),
            parameters=deepcopy(_INPUT_SCHEMA),
            output_schema=deepcopy(_OUTPUT_SCHEMA),
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        self._client_factory = client_factory
        self._cluster_hint = cluster_hint

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Transfer the artifact and return only its local reference, never content."""

        payload = await fetch_relay_artifact(
            self._client_factory, arguments, cluster_hint=self._cluster_hint
        )
        origin = payload["origin"]
        summary = (
            f"Transferred {payload['size_bytes']} bytes to {payload['local_path']} "
            f"from relay artifact {origin['artifact_id']} "
            f"(job {origin['job_id']}, cluster {origin['cluster'] or 'unknown'})."
        )
        return ToolResult(content=summary, structured_content=payload)


__all__ = [
    "RELAY_ARTIFACT_CONTENT_LIMIT_BYTES",
    "RELAY_FETCH_BARE_NAME",
    "RELAY_FETCH_ORIGIN_SCHEMA",
    "RELAY_FETCH_TOOL_NAME",
    "RelayArtifactFetchError",
    "RelayArtifactFetchTool",
    "fetch_max_bytes",
    "fetch_relay_artifact",
]
