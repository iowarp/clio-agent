"""Write a dashboard's offline HTML by streaming, with its files by reference (#1823).

The client's snapshot names each file a view reads as ``{"artifact": "<artifact_id>"}``
instead of carrying its bytes. Here each named artifact is resolved through the same
byte ladder that serves ``/v1/artifacts/{id}/bytes``, and its bytes are copied into the
page in chunks, base64-encoded, as an inert data block (``<script
type="application/octet-stream" id="clio-bytes-N">``) that the offline viewer decodes
only when a view asks for it. The snapshot entry becomes ``{"embedded": "clio-bytes-N"}``.

Memory stays at a chunk, not at the sum of the files: nothing builds the page as one
string, and no file is held whole. A page larger than
``artifacts.dashboard_export_max_bytes`` is refused before it is written
(``dashboard_export_too_large``), naming its size.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from fastapi import FastAPI, HTTPException

from clio_agent import conf

#: Bytes read per step; a multiple of 3 so each step's base64 has no padding.
CHUNK_BYTES = 3 * 1024 * 1024
#: Default page budget. Unit: bytes (2 GiB).
DEFAULT_DASHBOARD_MAX_BYTES = 2 * 1024**3
#: Where the inert data blocks go in the page body.
_EMBED_MARK = "<!--clio-embedded-files-->"


class DashboardExportRefused(ValueError):
    """A typed refusal of an export: ``code`` is stable, ``details`` name the cause."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def dashboard_max_bytes() -> int:
    """The largest offline dashboard page written; zero means unlimited."""

    value = conf.resolve(
        "artifacts.dashboard_export_max_bytes",
        env="CLIO_DASHBOARD_EXPORT_MAX_BYTES",
        default=DEFAULT_DASHBOARD_MAX_BYTES,
        cast=conf.as_int,
    )
    if value < 0:
        raise ValueError(
            "artifacts.dashboard_export_max_bytes must be zero (unlimited) or positive"
        )
    return int(value)


@dataclass(frozen=True)
class EmbeddedFile:
    """One artifact's bytes to copy into the page."""

    element_id: str
    artifact_id: str
    path: Path
    sha256: str | None
    size: int


def _resolved_path(app: FastAPI, artifact_id: str, workspace_id: str) -> tuple[Path, str | None]:
    """The verified file behind ``artifact_id``, through the one byte-serving ladder."""

    from clio_agent.gact.artifacts.registry import get_registry  # noqa: PLC0415
    from clio_agent.gact.routes.artifacts import _serve_bytes  # noqa: PLC0415

    found = get_registry(app).get_by_artifact_id(artifact_id)
    if found is None or found[0].workspace_id != workspace_id:
        raise DashboardExportRefused(
            "dashboard_export_file_unavailable",
            f"artifact {artifact_id} is not in this session's workspace",
            artifact_id=artifact_id,
        )
    record, version = found
    try:
        path = _serve_bytes(app, record, version, consume=lambda source, *_: Path(source))
    except HTTPException as exc:
        error: dict[str, Any] = (
            ((exc.detail or {}).get("error") or {}) if isinstance(exc.detail, dict) else {}
        )
        if error.get("error") != "custody_not_cas":
            raise DashboardExportRefused(
                "dashboard_export_file_unavailable",
                f"artifact {artifact_id} bytes are not readable: {error.get('message', exc)}",
                artifact_id=artifact_id,
            ) from exc
        # Workspace-referenced: the ladder re-hashed the project file before saying so.
        path = Path(version.path)
    return path, version.sha256


def embed_artifacts(
    app: FastAPI, sid: str, snapshot: dict[str, Any]
) -> tuple[dict[str, Any], list[EmbeddedFile]]:
    """The snapshot with every ``{"artifact": id}`` response turned into an embedded
    block reference, and the files to copy (one block per distinct artifact)."""

    from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

    workspace_id = _session_workspace_id(app, sid)
    responses = dict(snapshot.get("responses") or {})
    files: dict[str, EmbeddedFile] = {}
    for key, response in responses.items():
        if not isinstance(response, dict) or not isinstance(response.get("artifact"), str):
            continue
        artifact_id = response["artifact"]
        if artifact_id not in files:
            path, sha256 = _resolved_path(app, artifact_id, workspace_id)
            files[artifact_id] = EmbeddedFile(
                element_id=f"clio-bytes-{len(files)}",
                artifact_id=artifact_id,
                path=path,
                sha256=sha256,
                size=path.stat().st_size,
            )
        responses[key] = {"embedded": files[artifact_id].element_id}
    return {**snapshot, "responses": responses}, list(files.values())


def _encoded_size(size: int) -> int:
    return 4 * ((size + 2) // 3)


def _write_block(output: IO[bytes], item: EmbeddedFile) -> None:
    output.write(f'<script type="application/octet-stream" id="{item.element_id}">'.encode("ascii"))
    digest = hashlib.sha256()
    with open(item.path, "rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            output.write(base64.b64encode(chunk))
    if item.sha256 and digest.hexdigest() != item.sha256:
        raise DashboardExportRefused(
            "dashboard_export_file_changed",
            f"artifact {item.artifact_id} no longer matches its recorded hash",
            artifact_id=item.artifact_id,
        )
    output.write(b"</script>")


def write_dashboard_html(
    target: Path,
    *,
    report: dict[str, Any],
    snapshot: dict[str, Any],
    files: list[EmbeddedFile],
    renderer: dict[str, Any],
) -> int:
    """Write the offline page to ``target`` and return its size in bytes."""

    from clio_agent.gact.session_export_document import escape  # noqa: PLC0415
    from clio_agent.gact.session_export_viewer import _page  # noqa: PLC0415

    def encoded(value: bytes) -> str:
        return json.dumps(base64.b64encode(gzip.compress(value, mtime=0)).decode())

    bootstrap = (
        "window.CLIO_DASHBOARD_DATA="
        + encoded(json.dumps({"report": report, "snapshot": snapshot}).encode())
        + ";"
    )
    loader = (
        "new Response(new Blob([Uint8Array.from(atob("
        + encoded(renderer["javascript"].encode())
        + "),c=>c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip')))"
        ".text().then(source=>(0,eval)(source)).catch(error=>{document.getElementById("
        "'archive-status').textContent=String(error);});"
    )
    fallback = (
        '<div id="archive-document"><h1>'
        + escape(report["title"])
        + "</h1><p>Saved dashboard. Enable JavaScript to inspect its interactive views.</p>"
        + "<details><summary>Report definition and source revisions</summary><pre>"
        + escape(json.dumps(report, ensure_ascii=False, indent=2))
        + "</pre></details></div>"
    )
    page = _page(
        report["title"],
        '<div id="archive-status" role="status"></div>'
        + fallback
        + '<div id="root" hidden></div>'
        + _EMBED_MARK,
        stylesheet=renderer["stylesheet"],
        scripts=(bootstrap, loader),
    )
    head, tail = (part.encode() for part in page.split(_EMBED_MARK, 1))
    total = len(head) + len(tail)
    total += sum(_encoded_size(item.size) + 64 + len(item.element_id) for item in files)
    budget = dashboard_max_bytes()
    if budget and total > budget:
        raise DashboardExportRefused(
            "dashboard_export_too_large",
            f"the offline dashboard would be {total} bytes, over the {budget}-byte budget "
            "(artifacts.dashboard_export_max_bytes); export a dashboard with fewer or smaller "
            "files, or raise the budget",
            total_bytes=total,
            budget_bytes=budget,
            files=len(files),
        )
    with open(target, "wb") as output:
        output.write(head)
        for item in files:
            _write_block(output, item)
        output.write(tail)
    return target.stat().st_size


__all__ = [
    "DEFAULT_DASHBOARD_MAX_BYTES",
    "DashboardExportRefused",
    "EmbeddedFile",
    "dashboard_max_bytes",
    "embed_artifacts",
    "write_dashboard_html",
]
