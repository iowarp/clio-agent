"""Portable, fresh write/readback probes for the managed monitoring backends."""

from __future__ import annotations

import contextlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


def verify_flowcept(root: Path, url: str, probe: str) -> dict[str, Any]:
    """Publish through Flowcept's transport and read the exact task through its API."""
    from stack import flowcept_environment  # type: ignore[import-not-found]

    flowcept_environment(root)
    from flowcept import Flowcept
    from flowcept.instrumentation.task_capture import FlowceptTask

    with Flowcept(
        workflow_id=probe,
        workflow_name="CLIO setup verification",
        start_persistence=False,
        check_safe_stops=False,
    ):
        task = FlowceptTask(
            task_id=probe,
            workflow_id=probe,
            activity_id="clio.setup.verify",
            generated={"verification_id": probe},
            capture_telemetry=False,
        )
        del task
    deadline = time.monotonic() + 30
    with httpx.Client(timeout=10, trust_env=False) as client:
        while time.monotonic() < deadline:
            response = client.post(
                url + "/api/v1/query/tasks", json={"filter": {"task_id": probe}, "limit": 1}
            )
            response.raise_for_status()
            rows = response.json()["items"]
            if (
                rows
                and rows[0].get("task_id") == probe
                and rows[0].get("generated", {}).get("verification_id") == probe
            ):
                return {"task_id": probe, "workflow_id": probe, "write_readback": True}
            time.sleep(0.5)
    raise RuntimeError("The fresh Flowcept task did not reach its query API")


def verify_cmf(manifest: dict[str, Any], url: str, probe: str) -> dict[str, Any]:
    """Read back the exact execution, input and output artifact lineage."""
    document = json.loads(
        json.dumps(manifest["verification_document"]).replace("CLIO_SETUP_PROBE", probe)
    )
    pipeline = document["Pipeline"][0]["name"]
    wanted = document["Pipeline"][0]["stages"][0]["executions"][0]
    execution = wanted["properties"]["Execution_uuid"]
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.post(
            url + "/api/mlmd_push",
            json={
                "exec_uuid": None,
                "pipeline_name": pipeline,
                "json_payload": json.dumps(document),
            },
        )
        response.raise_for_status()
        if response.json().get("status") not in {"success", "exists"}:
            raise RuntimeError("CMF refused the setup provenance document")
        response = client.post(
            url + "/mlmd_pull", json={"pipeline_name": pipeline, "exec_uuid": execution}
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, str):
            payload = json.loads(payload)
        for item in payload.get("Pipeline", []):
            for stage in item.get("stages", []):
                for held in stage.get("executions", []):
                    identifiers = held.get("properties", {}).get("Execution_uuid", "").split(",")
                    edges = {
                        (event["type"], event["artifact"]["uri"])
                        for event in held.get("events", [])
                    }
                    expected = {
                        (event["type"], event["artifact"]["uri"]) for event in wanted["events"]
                    }
                    if execution in identifiers and expected <= edges:
                        return {
                            "execution_id": execution,
                            "pipeline": pipeline,
                            "input_output_lineage": True,
                            "write_readback": True,
                        }
    raise RuntimeError("CMF returned success without retaining the expected artifact lineage")


def main() -> None:
    """Return sanitized evidence only after the fresh backend-specific probe passes."""
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text())
    probe = str(uuid.uuid4())
    url = f"http://127.0.0.1:{manifest['port']}"
    with contextlib.redirect_stdout(sys.stderr):
        evidence = (
            verify_flowcept(root, url, probe)
            if manifest["service"] == "flowcept"
            else verify_cmf(manifest, url, probe)
        )
    print(
        json.dumps(
            {
                **evidence,
                "service": manifest["service"],
                "provenance_ingesting": True,
                "verified_at": time.time(),
            }
        )
    )


if __name__ == "__main__":
    main()
