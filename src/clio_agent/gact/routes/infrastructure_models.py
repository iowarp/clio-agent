"""Model discovery and durable acquisition on the explicitly selected execution host."""

from __future__ import annotations

import hashlib
import json
import posixpath
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query

from clio_agent.gact.infrastructure import node_models, reuse
from clio_agent.gact.infrastructure.model_registry import (
    ModelAcquisition,
    ModelDownloadRequest,
    search_models,
)
from clio_agent.gact.infrastructure.models import CommandSpec
from clio_agent.gact.infrastructure.operation_models import ReuseReport
from clio_agent.gact.infrastructure.operation_progress import redact
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.storage import resolved_locations


def missing_receipt(prior: ModelAcquisition) -> ModelAcquisition:
    """Relabel a record whose host receipt is gone, never calling a finished download interrupted.

    A verified model is not a stopped download: without a receipt this host cannot vouch
    for its files, so it is stale (retry re-verifies) rather than interrupted.
    """
    finished = prior.state in {"ready", "stale"} or (
        prior.bytes_total is not None
        and prior.bytes_done == prior.bytes_total
        and prior.phase.startswith("Model available")
    )
    return prior.model_copy(
        update={
            "state": "stale" if finished else "interrupted",
            "error_code": "receipt_missing",
            "error": (
                "This host has no download receipt for this model (downloaded on another "
                "host, or the receipt was removed); retry to verify its files here."
                if finished
                else "The host download receipt is missing; retry will reverify the "
                "recorded revision."
            ),
        }
    )


def register_infrastructure_model_routes(app: FastAPI) -> None:
    """Reuse CLIO's target transport; detached downloads survive client disconnection."""

    async def execute(
        target_id: str, action: str, body: dict[str, Any], *, root: str | None = None
    ) -> Any:
        target = app.state.infrastructure_store.target(target_id)
        if target is None:
            raise HTTPException(404, "Execution host not found")
        runtime = app.state.infrastructure_runtime

        async def run(spec: CommandSpec) -> Any:
            return await runtime.execute_on_target(target_id, spec)

        try:
            selected_root = root
            if selected_root is None:
                facts = await probe_target(target, run if target.kind == "ssh" else None)
                if facts.transport_state != "connected":
                    raise ValueError("Connect this execution host to inspect and download models.")
                if facts.os != "linux":
                    raise ValueError(
                        "Managed model downloads currently require a Linux execution host"
                    )
                locations = resolved_locations(target, facts)
                selected_root = locations.root
                if action == "start" and not body.get("destination"):
                    suffix = (
                        body["repository"].replace("/", "--")
                        + "--"
                        + body["revision"].replace("/", "--")
                    )
                    if body.get("files"):
                        # A file selection is its own acquisition (and directory).
                        selection = "\0".join(body["files"]).encode()
                        suffix += "--" + hashlib.sha256(selection).hexdigest()[:8]
                    body["destination"] = posixpath.join(locations.models, suffix)
            if action == "start":
                app.state.infrastructure_store.register_model_root(target_id, selected_root)
            script = Path(node_models.__file__).read_text(encoding="utf-8")
            payload = {**body, "root": selected_root, "action": action}
            if action == "start":
                payload["script"] = script
            result = await run(
                CommandSpec(
                    program="python3",
                    args=["-c", script],
                    stdin=json.dumps(payload),
                    timeout_seconds=30,
                )
            )
            if result.exit_code:
                raise ValueError(
                    result.stderr.strip().splitlines()[-1]
                    if result.stderr.strip()
                    else "Host model operation failed"
                )
            rows = json.loads(result.stdout)
            if action == "log":
                return rows
            if action == "start" and rows.pop("reused", False):
                rows["reuse"] = _reuse_report(rows)
            if action == "list":
                found = {row["id"] for row in rows}
                for prior in app.state.infrastructure_store.model_acquisitions(target_id):
                    if prior.storage_root == selected_root and prior.id not in found:
                        app.state.infrastructure_store.put_model_acquisition(missing_receipt(prior))
            for row in rows if action == "list" else [rows]:
                observed = ModelAcquisition.model_validate(
                    {**row, "target_id": target_id, "storage_root": selected_root}
                )
                app.state.infrastructure_store.put_model_acquisition(observed)
            return rows
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/infrastructure/models/search")
    async def search(q: str = Query(min_length=1, max_length=180)) -> dict[str, Any]:
        """Search the public registry; host credentials stay on the execution side."""
        try:
            return {"models": await search_models(q)}
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, "Model registry is unavailable; retry or use an exact repository"
            ) from exc

    @app.get("/v1/infrastructure/targets/{target_id}/models")
    async def inventory(target_id: str) -> dict[str, Any]:
        """Inspect cached models and live/interrupted download receipts on their host."""
        store = app.state.infrastructure_store
        target = store.target(target_id)
        if target is None:
            raise HTTPException(404, "Execution host not found")
        unavailable = None
        try:

            async def run(spec: CommandSpec) -> Any:
                return await app.state.infrastructure_runtime.execute_on_target(target_id, spec)

            facts = await probe_target(target, run if target.kind == "ssh" else None)
            if facts.transport_state != "connected":
                unavailable = "Connect this execution host to inspect and download models."
            elif facts.os != "linux":
                unavailable = "Managed model downloads require a Linux execution host. Select a connected Linux host."
            elif not facts.uv_available:
                unavailable = "Install uv on this execution host before downloading models."
        except (OSError, ValueError, RuntimeError):
            unavailable = "Connect this execution host to inspect and download models."
        errors = []
        for root in store.model_roots(target_id):
            try:
                await execute(target_id, "list", {}, root=root)
            except HTTPException as exc:
                errors.append({"storage_root": root, "error": exc.detail})
        return {
            "target_id": target_id,
            "models": [row.model_dump() for row in store.model_acquisitions(target_id)],
            "errors": errors,
            "unavailable_reason": unavailable,
        }

    @app.post("/v1/infrastructure/targets/{target_id}/models", status_code=202)
    async def acquire(target_id: str, body: ModelDownloadRequest) -> dict[str, Any]:
        """Resolve/download/verify one model; this never starts inference."""
        return await execute(target_id, "start", body.model_dump())

    @app.post("/v1/infrastructure/targets/{target_id}/models/{job_id}/cancel")
    async def cancel(target_id: str, job_id: str) -> dict[str, Any]:
        """Cancel only the recorded acquisition; keep all reusable model bytes."""
        row = next(
            (
                row
                for row in app.state.infrastructure_store.model_acquisitions(target_id)
                if row.id == job_id
            ),
            None,
        )
        if row is None:
            raise HTTPException(404, "Model operation not found on this execution host")
        return await execute(target_id, "cancel", {"id": job_id}, root=row.storage_root)

    @app.get("/v1/infrastructure/targets/{target_id}/models/{job_id}/log")
    async def download_log(target_id: str, job_id: str, offset: int = 0) -> dict[str, Any]:
        """Whole lines of the download's log from byte ``offset`` (redacted).

        Poll with ``offset`` set to the previous reply's ``next_offset``; a log
        that restarted is read again from 0.
        """
        row = next(
            (
                row
                for row in app.state.infrastructure_store.model_acquisitions(target_id)
                if row.id == job_id
            ),
            None,
        )
        if row is None:
            raise HTTPException(404, "Model operation not found on this execution host")
        chunk = await execute(
            target_id, "log", {"id": job_id, "offset": max(0, offset)}, root=row.storage_root
        )
        chunk["text"] = "\n".join(redact(line) for line in chunk["text"].split("\n"))
        return chunk

    @app.post("/v1/infrastructure/targets/{target_id}/models/{job_id}/retry", status_code=202)
    async def retry(target_id: str, job_id: str) -> dict[str, Any]:
        """Resume the original immutable revision and location, even after defaults change."""
        row = next(
            (
                row
                for row in app.state.infrastructure_store.model_acquisitions(target_id)
                if row.id == job_id
            ),
            None,
        )
        if row is None:
            raise HTTPException(404, "Model operation not found on this execution host")
        return await execute(
            target_id,
            "start",
            {
                "repository": row.repository,
                "revision": row.requested_revision,
                "destination": row.destination,
                "resolved_revision": row.revision,
                "files": row.files,
            },
            root=row.storage_root,
        )


def _reuse_report(row: dict[str, Any]) -> dict[str, Any]:
    """The reuse of a verified model revision a download found already present."""

    found = reuse.Reuse(
        kind="model",
        thing=f"model {row.get('repository', '')}".strip(),
        identity=str(row.get("revision") or row.get("requested_revision") or ""),
        path=str(row.get("destination") or ""),
        size_bytes=row.get("bytes_total"),
    )
    return ReuseReport(
        kind=found.kind,
        thing=found.thing,
        identity=found.identity,
        path=found.path,
        size_bytes=found.size_bytes,
        message=found.message(),
    ).model_dump(mode="json")
