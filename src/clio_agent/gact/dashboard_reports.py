"""Agent-authored dashboard artifacts; composition remains ordinary trusted A2UI."""

from __future__ import annotations

import base64
import gzip
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from clio_agent import paths
from clio_agent.gact import context
from clio_agent.gact.artifacts.minting import (
    _contained,
    _session_workspace_id,
    _workspace_root,
    artifact_name_for_path,
    mint_artifact_outcome,
)
from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import ingest_artifact_identity, resolve_owned_artifact_path
from clio_agent.gact.session_export_document import escape
from clio_agent.gact.session_export_viewer import _page
from clio_agent.platform_paths import win_extended_path


def _store(
    app: FastAPI, sid: str, report: dict[str, Any], *, payload: bytes, extension: str
) -> str:
    """Register real workspace bytes through the ordinary artifact mint funnel."""
    wid = _session_workspace_id(app, sid)
    root = _workspace_root(app, wid)
    if root is None:
        raise ValueError("This session has no workspace in which to save the report.")
    report_id = str(uuid.UUID(report["id"]))
    target = (
        paths.workspace_state_dir(root) / "reports" / f"{report_id}.dashboard.{extension}"
    ).resolve()
    if not _contained(target, root):
        raise ValueError("The report storage directory resolves outside the workspace.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    ingested = ingest_artifact_identity(app, target, workspace_root=root)
    outcome = mint_artifact_outcome(
        app,
        sid,
        name=artifact_name_for_path(target),
        workspace_id=wid,
        evidence=ingested.evidence,
        kind=ArtifactKind.REPORT,
        mechanism=Mechanism.HARNESS,
        turn_id=context.active_turn_id(),
        trace_id=context.active_trace_id(),
        producing=extension == "json",
        producer={
            "designation": "dashboard-report" if extension == "json" else "dashboard-export",
            "session_id": sid,
            "report_id": report_id,
            "title": report["title"],
            "definition_path": report["definition_path"],
        },
        custody=ingested.custody,
        path=str(target),
        ingested=ingested,
        not_ingested_size=ingested.not_ingested_size,
    )
    if outcome is None:
        raise ValueError("The report could not be registered as an artifact.")
    return outcome.version.artifact_id


def publish_dashboard_report(
    app: FastAPI,
    sid: str,
    *,
    definition_path: str,
    report_id: str = "",
) -> dict[str, Any]:
    """Publish an authored A2UI document as a versioned artifact beside chat."""
    from clio_agent.gact.dashboard_document import (  # noqa: PLC0415
        compile_dashboard_surface,
        read_dashboard_document,
    )

    document = read_dashboard_document(app, sid, definition_path)
    sources = []
    for source_id in dict.fromkeys(document.source_surface_ids):
        source = app.state.a2ui_store.get(sid, source_id)
        if source is None or source.state == "deleted":
            raise ValueError(f"Source view is unavailable: {source_id}")
        sources.append({"surface_id": source.id, "revision": source.revision})
    if report_id:
        report_id = str(uuid.UUID(report_id))
        if not any(item["id"] == report_id for item in list_dashboard_reports(app, sid)):
            raise ValueError("The report to revise does not belong to this session.")
    else:
        report_id = str(uuid.uuid4())
    surface, export_report = compile_dashboard_surface(app, sid, document, report_id)
    report = {
        "format": "clio.dashboard.v1",
        "id": report_id,
        "session_id": sid,
        "title": document.title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "surface": surface,
        "definition": document.model_dump(),
        "definition_path": definition_path,
        "sources": sources,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2).encode()
    artifact_id = _store(app, sid, report, payload=payload, extension="json")
    return {
        "id": report_id,
        "title": document.title,
        "created_at": report["created_at"],
        "artifact_id": artifact_id,
        "uri": f"artifact://{artifact_id}",
        "definition_path": definition_path,
        "components": len(document.components),
        **export_report,
    }


def list_dashboard_reports(app: FastAPI, sid: str) -> list[dict[str, Any]]:
    """Read report identities from the persistent artifact registry, newest first."""
    wid = _session_workspace_id(app, sid)
    latest: dict[str, dict[str, Any]] = {}
    for record in get_registry(app).list_for_workspace(wid):
        for version in record.versions:
            producer = version.producer
            if (
                producer.get("designation") != "dashboard-report"
                or producer.get("session_id") != sid
            ):
                continue
            report_id = str(producer["report_id"])
            row = {
                "id": report_id,
                "title": producer["title"],
                "created_at": version.created_at,
                "artifact_id": version.artifact_id,
            }
            if report_id not in latest or row["created_at"] >= latest[report_id]["created_at"]:
                latest[report_id] = row
    return sorted(latest.values(), key=lambda row: row["created_at"], reverse=True)


def read_dashboard_report(app: FastAPI, sid: str, artifact_id: str) -> dict[str, Any]:
    """Read a pinned report version only from the owning session's verified bytes."""
    found = get_registry(app).get_by_artifact_id(artifact_id)
    if found is None:
        raise ValueError("This dashboard report is unavailable.")
    record, version = found
    if (
        version.producer.get("designation") != "dashboard-report"
        or version.producer.get("session_id") != sid
    ):
        raise ValueError("This dashboard report does not belong to this session.")
    root = _workspace_root(app, record.workspace_id)
    path = resolve_owned_artifact_path(app, version, workspace_root=root)
    if path is None:
        raise ValueError("The saved dashboard bytes are unavailable.")
    return json.loads(Path(win_extended_path(path)).read_text(encoding="utf-8"))


def export_dashboard_report(
    app: FastAPI, sid: str, artifact_id: str, renderer: dict[str, Any]
) -> tuple[Path, str]:
    """Bundle chosen content and captured source data into one offline HTML artifact."""
    report = read_dashboard_report(app, sid, artifact_id)
    snapshot = renderer["snapshot"]
    if snapshot.get("failures"):
        raise ValueError(
            "Report dependencies could not all be captured: " + json.dumps(snapshot["failures"])
        )
    captured = snapshot.get("sessions", {}).get(sid)
    # Clients decode the shared surface schema and omit server-only timestamps,
    # eviction counters and action history. Compare identity and rendered content
    # rather than requiring those transport metadata fields to survive decoding.
    content_keys = ("id", "session_id", "catalog_id", "protocol_version", "revision", "messages")
    if (
        not isinstance(captured, list)
        or len(captured) != 1
        or not isinstance(captured[0], dict)
        or any(captured[0].get(key) != report["surface"].get(key) for key in content_keys)
    ):
        raise ValueError("Export must capture exactly the published dashboard.")

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
        ".text().then(source=>(0,eval)(source)).catch(error=>{document.getElementById('archive-status').textContent=String(error);});"
    )
    fallback = (
        '<div id="archive-document"><h1>'
        + escape(report["title"])
        + "</h1><p>Saved dashboard. Enable JavaScript to inspect its interactive views.</p>"
        + "<details><summary>Report definition and source revisions</summary><pre>"
        + escape(json.dumps(report, ensure_ascii=False, indent=2))
        + "</pre></details></div>"
    )
    html = _page(
        report["title"],
        '<div id="archive-status" role="status"></div>' + fallback + '<div id="root" hidden></div>',
        stylesheet=renderer["stylesheet"],
        scripts=(bootstrap, loader),
    )
    exported_id = _store(app, sid, report, payload=html.encode(), extension="html")
    # Downloads use a disposable copy; the artifact's verified bytes stay retained.
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    fd, filename = tempfile.mkstemp(prefix="clio-dashboard-", suffix=".html")
    with os.fdopen(fd, "wb") as output:
        output.write(html.encode())
    return Path(filename), exported_id
