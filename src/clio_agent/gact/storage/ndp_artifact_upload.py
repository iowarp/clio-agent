"""Publish pinned artifact bytes through an Endpoint's advertised S3 storage API."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import quote

import httpx

from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import resolve_owned_artifact_path
from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.models import StorageModel, now
from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnection, EndpointRequest
from clio_agent.gact.storage.ndp_endpoint_operations import resolve_request
from clio_agent.gact.storage.ndp_endpoint_transport import endpoint_client
from clio_agent.gact.storage.store import SourceStore

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_MEMORY_BYTES = 1024 * 1024
_CHUNK_BYTES = 65536


class EndpointUploadReceipt(StorageModel):
    """Durable publication evidence, including an explicitly unverified write."""

    id: str
    workspace_id: str
    endpoint_id: str
    endpoint_url: str
    ssh_profile: str
    ssh_route: SshRoute | None = None
    artifact_id: str
    sha256: str
    size: int
    bucket: str
    key: str
    started_at: str
    state: str
    resource: dict[str, Any]
    reused: bool = False
    completed_at: str = ""


@dataclass
class ArtifactUpload:
    """An immutable byte snapshot and its exact approved storage destination."""

    artifact_id: str
    filename: str
    sha256: str
    size: int
    content_type: str
    bucket: str
    key: str
    upload_url: str
    read_url: str
    handle: BinaryIO


@contextmanager
def prepare_artifact_upload(
    app: Any,
    workspace_id: str,
    connection: EndpointConnection,
    artifact_id: str,
    bucket_name: str,
    key_prefix: str,
) -> Iterator[ArtifactUpload]:
    """Resolve a workspace-owned version, validate storage, and snapshot verified bytes.

    Snapshot before approval so an edited workspace file cannot change what the
    user approved. Memory and file size are bounded; large snapshots spill to a
    temporary file that is closed on denial, failure or completion.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket_name):
        raise ValueError("Choose an existing S3 bucket name")
    prefix = key_prefix.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", prefix) or any(
        part in {"", ".", ".."} for part in prefix.split("/")
    ):
        raise ValueError("Choose a storage key prefix without relative path segments")
    match = get_registry(app).get_by_artifact_id(artifact_id)
    if match is None or match[0].workspace_id != workspace_id:
        raise ValueError("Choose an exact artifact version from this workspace")
    record, version = match
    if not version.sha256 or version.size_bytes is None:
        raise ValueError("Register and hash the artifact before uploading it")
    if version.size_bytes > MAX_UPLOAD_BYTES:
        raise ValueError("Use an approved bulk transfer for artifacts larger than 512 MiB")
    workspace = app.state.workspaces.get(workspace_id)
    root_path = getattr(workspace, "root_path", "") if workspace is not None else ""
    root = Path(root_path).resolve() if root_path else None
    source = resolve_owned_artifact_path(app, version, workspace_root=root)
    if source is None:
        receipt = (version.producer or {}).get("storage_receipt")
        if isinstance(receipt, dict) and receipt.get("provider") == "cmf":
            raise ValueError("The artifact's recorded CMF object is unavailable")
        source = Path(version.path).resolve() if version.path else None
        if source is None or root is None or not source.is_relative_to(root):
            raise PermissionError("Artifact bytes must belong to the selected workspace")
    if version.size_bytes > _MEMORY_BYTES and (
        shutil.disk_usage(tempfile.gettempdir()).free < version.size_bytes + 16 * _MEMORY_BYTES
    ):
        raise ValueError("Not enough temporary storage to snapshot this artifact for upload")
    filename = Path(version.path or record.name).name
    filename = re.sub(r'[\x00-\x1f\x7f/\\"]', "_", filename).strip(". ")[:180] or "artifact"
    key = f"{prefix}/{version.sha256}/{filename}"
    operation, upload_url, _ = resolve_request(
        connection,
        EndpointRequest(
            operation_id="POST /s3/objects/{bucket_name}",
            parameters={"bucket_name": bucket_name},
        ),
        multipart=True,
    )
    schema = operation["multipart_schema"]
    properties = schema.get("properties", {})
    file_schema = properties.get("file", {})
    binary_file = file_schema.get("format") == "binary" or (
        file_schema.get("type") == "string"
        and file_schema.get("contentMediaType") == "application/octet-stream"
        and not file_schema.get("contentEncoding")
    )
    if (
        not binary_file
        or "object_key" not in properties
        or set(schema.get("required", [])) - {"file", "object_key"}
    ):
        raise ValueError("This Endpoint does not advertise the supported S3 artifact upload")
    _, read_url, _ = resolve_request(
        connection,
        EndpointRequest(
            operation_id="GET /s3/objects/{bucket_name}/{object_key}",
            parameters={"bucket_name": bucket_name, "object_key": key},
        ),
    )
    digest, size = hashlib.sha256(), 0
    with tempfile.SpooledTemporaryFile(max_size=_MEMORY_BYTES, mode="w+b") as snapshot:
        with source.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > version.size_bytes or size > MAX_UPLOAD_BYTES:
                    raise ValueError("Artifact bytes changed since this version was registered")
                snapshot.write(chunk)
                digest.update(chunk)
        if size != version.size_bytes or digest.hexdigest() != version.sha256:
            raise ValueError("Artifact bytes changed since this version was registered")
        snapshot.seek(0)
        yield ArtifactUpload(
            artifact_id=artifact_id,
            filename=filename,
            sha256=version.sha256,
            size=size,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            bucket=bucket_name,
            key=key,
            upload_url=upload_url,
            read_url=read_url,
            handle=cast(BinaryIO, snapshot),
        )


def _read_back(client: httpx.Client, upload: ArtifactUpload, headers: dict[str, str]) -> bool:
    """Verify actual stored bytes; a missing object is the only safe create case."""
    deadline = time.monotonic() + 120
    with client.stream("GET", upload.read_url, headers=headers) as response:
        if response.status_code == 404:
            return False
        if response.status_code in {401, 403}:
            raise PermissionError("NDP storage access denied; check the Endpoint's write role")
        response.raise_for_status()
        digest, size = hashlib.sha256(), 0
        for chunk in response.iter_bytes(chunk_size=_CHUNK_BYTES):
            size += len(chunk)
            if size > upload.size or time.monotonic() > deadline:
                raise ValueError(
                    "The stored object could not be verified within its byte/time limit"
                )
            digest.update(chunk)
        if size != upload.size or digest.hexdigest() != upload.sha256:
            raise ValueError("Stored object differs from this artifact; it was not overwritten")
        return True


def upload_artifact(
    store: SourceStore,
    connection: EndpointConnection,
    upload: ArtifactUpload,
    token: str | None,
) -> dict[str, Any]:
    """Upload once, read back the bytes, and retain a credential-free receipt.

    A content-addressed key permits safe retries: verify an existing object
    instead of sending the file again. A failed or lost write response leaves an
    explicit unverified receipt; it never reports a completed dataset.
    """
    headers = {}
    if connection.authentication == "ndp":
        if not token:
            raise PermissionError("Sign in to NDP before uploading artifacts")
        headers["Authorization"] = "Bearer " + token
    receipt_id = "ndpupload_" + uuid.uuid4().hex
    receipt = {
        "id": receipt_id,
        "workspace_id": connection.workspace_id,
        "endpoint_id": connection.id,
        "endpoint_url": connection.url,
        "ssh_profile": connection.ssh_profile,
        "ssh_route": connection.ssh_route.model_dump() if connection.ssh_route else None,
        "artifact_id": upload.artifact_id,
        "sha256": upload.sha256,
        "size": upload.size,
        "bucket": upload.bucket,
        "key": upload.key,
        "started_at": now(),
        "state": "unverified",
        "resource": {
            "url": f"s3://{upload.bucket}/{quote(upload.key, safe='/')}",
            "name": upload.filename,
            "format": Path(upload.filename).suffix.lstrip(".").upper(),
            "mimetype": upload.content_type,
            "size": upload.size,
        },
    }
    with endpoint_client(connection, 120) as client:
        reused = _read_back(client, upload, headers)
        if not reused:
            store.put("ndp_upload", receipt_id, EndpointUploadReceipt.model_validate(receipt))
            try:
                with client.stream(
                    "POST",
                    upload.upload_url,
                    headers=headers,
                    data={"object_key": upload.key},
                    files={"file": (upload.filename, upload.handle, upload.content_type)},
                ) as response:
                    if response.status_code in {401, 403}:
                        raise PermissionError("NDP upload denied; check the Endpoint's write role")
                    response.raise_for_status()
                    # The upload response is metadata; verify the object itself below.
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > _CHUNK_BYTES:
                            raise ValueError("NDP upload response exceeds the 64 KiB receipt limit")
                if not _read_back(client, upload, headers):
                    raise ValueError("The uploaded object is missing during read-back")
            except PermissionError:
                raise
            except (httpx.HTTPError, OSError, ValueError) as exc:
                raise ValueError(
                    f"NDP upload is unverified (receipt {receipt['id']}); retry this exact "
                    "artifact/destination to check storage before writing again"
                ) from exc
    receipt.update(state="verified", reused=reused, completed_at=now())
    store.put("ndp_upload", receipt_id, EndpointUploadReceipt.model_validate(receipt))
    return receipt
