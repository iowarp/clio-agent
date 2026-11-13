"""``ui/download-file`` for MCP Apps: a view's export becomes a workspace file (MA16).

The draft MCP Apps specification (ext-apps ``specification/draft`` at ``ea3e9d0``)
defines ``ui/download-file`` with ``params.contents``: a list of MCP
``EmbeddedResource`` (``{type: "resource", resource: {uri, mimeType, text | blob}}``)
or ``ResourceLink`` (``{type: "resource_link", uri, name, mimeType}``) blocks. The
result is ``{isError?}`` plus passthrough fields.

CLIO saves each embedded resource into the session's workspace folder under a
sanitized name derived from the last segment of ``resource.uri``, never
overwriting an existing file, and registers it as an artifact. A resource link
is refused with a typed reason: fetching an address a sandboxed view chose from
CLIO's own process would let the view reach any host on the person's network.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.mcp_apps import MCPAppRecord

logger = logging.getLogger(__name__)

_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]+")


def download_max_bytes() -> int:
    """Byte ceiling for one ``ui/download-file`` request (all its files).

    Config: ``gact.mcp_apps.download_max_bytes`` / ``CLIO_MCP_APP_DOWNLOAD_MAX_BYTES``
    (default 64 MiB). A larger request is refused with ``download_too_large``.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "gact.mcp_apps.download_max_bytes",
        env="CLIO_MCP_APP_DOWNLOAD_MAX_BYTES",
        default=64 * 1024 * 1024,
        cast=conf.as_int,
    )


class DownloadRefused(ValueError):
    """A download CLIO will not save, with the typed reason the view receives."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _Pending:
    name: str
    data: bytes
    mime_type: str


def safe_file_name(uri: str, fallback: str = "download") -> str:
    """The last segment of ``uri``, made safe to create inside one folder."""

    parsed = urlparse(uri)
    segment = unquote((parsed.path or uri).rstrip("/").rsplit("/", 1)[-1])
    name = _UNSAFE_NAME.sub("_", segment).strip(" .")
    if not name or name in {".", ".."}:
        return fallback
    return name[:180]


def _decode(contents: Any) -> list[_Pending]:
    if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes)) or not contents:
        raise DownloadRefused("invalid_content", "ui/download-file needs a non-empty contents list")
    limit = download_max_bytes()
    total = 0
    pending: list[_Pending] = []
    for index, block in enumerate(contents):
        if not isinstance(block, Mapping):
            raise DownloadRefused("invalid_content", f"contents[{index}] is not an object")
        kind = block.get("type")
        if kind == "resource_link":
            raise DownloadRefused(
                "download_link_unsupported",
                "CLIO saves files the view sends; it does not fetch a linked address",
            )
        resource = block.get("resource")
        if kind != "resource" or not isinstance(resource, Mapping):
            raise DownloadRefused("invalid_content", f"contents[{index}] is not a resource")
        uri = str(resource.get("uri") or "")
        if isinstance(resource.get("text"), str):
            data = str(resource["text"]).encode("utf-8")
        elif isinstance(resource.get("blob"), str):
            try:
                data = base64.b64decode(str(resource["blob"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise DownloadRefused(
                    "invalid_content", f"contents[{index}].resource.blob is not base64"
                ) from exc
        else:
            raise DownloadRefused("invalid_content", f"contents[{index}] has no text or blob")
        total += len(data)
        if total > limit:
            raise DownloadRefused(
                "download_too_large", f"the download exceeds the {limit}-byte limit"
            )
        pending.append(
            _Pending(
                name=safe_file_name(uri, fallback=f"download-{index + 1}"),
                data=data,
                mime_type=str(resource.get("mimeType") or ""),
            )
        )
    return pending


def _exclusive_path(folder: Path, name: str) -> Path:
    stem, dot, suffix = name.partition(".") if not name.startswith(".") else (name, "", "")
    for attempt in range(1, 1000):
        candidate = name if attempt == 1 else f"{stem} ({attempt}){dot}{suffix}"
        path = folder / candidate
        if not path.exists():
            return path
    raise DownloadRefused("download_name_taken", f"no free name for {name!r} in the workspace")


def _write(folder: Path, item: _Pending) -> Path:
    while True:
        path = _exclusive_path(folder, item.name)
        try:
            with path.open("xb") as handle:
                handle.write(item.data)
            return path
        except FileExistsError:
            continue


def _mint(app: FastAPI, record: MCPAppRecord, workspace_id: str, root: Path, path: Path) -> str:
    """Register the saved file as an artifact; return its id or a typed skip reason."""

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.artifacts.designation import kind_for_path  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        artifact_name_for_path,
        mint_artifact_outcome,
    )
    from clio_agent.gact.artifacts.records import Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import (  # noqa: PLC0415
        harness_write_artifact_identity,
    )

    data = path.read_bytes()
    ingested = harness_write_artifact_identity(
        app,
        path,
        workspace_root=root,
        in_hand_sha=hashlib.sha256(data).hexdigest(),
        in_hand_size=len(data),
    )
    outcome = mint_artifact_outcome(
        app,
        record.session_id,
        name=artifact_name_for_path(path),
        workspace_id=workspace_id,
        evidence=ingested.evidence,
        kind=kind_for_path(path),
        mechanism=Mechanism.HARNESS,
        producer={
            "session_id": record.session_id,
            "tool": "mcp_app.download",
            "source_server": record.source_namespace or "",
            "app_tool": record.tool_name,
        },
        custody=ingested.custody,
        path=str(path),
        ingested=ingested,
        turn_id=_ctx.active_turn_id(),
        trace_id=_ctx.active_trace_id(),
        producing=False,
        not_ingested_size=ingested.not_ingested_size,
    )
    return outcome.version.artifact_id if outcome is not None else ""


def save_app_download(app: FastAPI, record: MCPAppRecord, params: Mapping[str, Any]) -> dict:
    """Save a view's ``ui/download-file`` contents into the session's workspace.

    Returns ``{files: [{name, path, artifact_id}]}`` with workspace-relative
    paths. Raises :class:`DownloadRefused` with a typed reason otherwise.
    """

    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
    )

    pending = _decode(params.get("contents"))
    workspace_id = _session_workspace_id(app, record.session_id)
    root = _workspace_root(app, workspace_id)
    if root is None or not root.is_dir():
        raise DownloadRefused("no_workspace", "this conversation has no workspace folder")
    saved: list[dict[str, str]] = []
    for item in pending:
        path = _write(root, item)
        try:
            artifact_id = _mint(app, record, workspace_id, root, path)
        except Exception:  # noqa: BLE001 - the file is saved; the artifact row is reported missing
            logger.warning(
                "mcp app download saved without an artifact reason=artifact_mint_failed path=%s",
                path,
            )
            artifact_id = ""
        saved.append(
            {
                "name": path.name,
                "path": path.relative_to(root).as_posix(),
                "artifact_id": artifact_id,
                **({} if artifact_id else {"artifact_skipped": "artifact_mint_failed"}),
            }
        )
    return {"files": saved}


__all__ = ["DownloadRefused", "download_max_bytes", "safe_file_name", "save_app_download"]
