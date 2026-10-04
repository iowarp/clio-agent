"""Isolated connection verifier; only allowlisted outcomes cross stdout."""

from __future__ import annotations

import contextlib
import json
import sys
import time
import uuid
from typing import Any

import httpx

from clio_agent.gact.infrastructure.models import ExternalServiceConnection
from clio_agent.gact.infrastructure.monitoring_services import verification_document
from clio_agent.gact.infrastructure.monitoring_verify import verify_cmf


def probe_flowcept(row: ExternalServiceConnection, probe: str) -> dict[str, Any]:
    """Emit through the configured MQ with a single external persistence owner."""
    from clio_agent.gact.provenance.flowcept import (
        FlowceptProvenanceProvider,
        FlowceptProviderConfig,
    )

    provider = FlowceptProvenanceProvider(
        FlowceptProviderConfig(
            settings_path=row.configuration["settings_path"],
            persistence_owner="collector",
            check_safe_stops=False,
        )
    )
    try:
        from flowcept.instrumentation.task_capture import FlowceptTask

        task = FlowceptTask(
            task_id=probe,
            workflow_id=probe,
            activity_id="clio.setup.verify",
            generated={"verification_id": probe},
            capture_telemetry=False,
        )
        del task
    finally:
        provider.close()
    with httpx.Client(timeout=10, trust_env=False) as client:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = client.post(
                row.url + "/api/v1/query/tasks", json={"filter": {"task_id": probe}, "limit": 1}
            )
            response.raise_for_status()
            if any(
                item.get("task_id") == probe
                and item.get("generated", {}).get("verification_id") == probe
                for item in response.json()["items"]
            ):
                return {"write_readback": True}
            time.sleep(0.5)
    raise ValueError("Flowcept write/readback failed")


def main() -> None:
    """Verify the selected backend independently and suppress private diagnostics."""
    try:
        row = ExternalServiceConnection.model_validate_json(sys.stdin.read())
        probe = str(uuid.uuid4())
        with contextlib.redirect_stdout(sys.stderr):
            if row.service_id == "cmf":
                evidence = verify_cmf(
                    {"verification_document": verification_document()}, row.url, probe
                )
            elif row.service_id == "flowcept":
                evidence = probe_flowcept(row, probe)
            else:
                raise ValueError("Unsupported provenance backend")
        print(json.dumps({"probe_id": probe, **evidence}))
    except Exception:  # noqa: BLE001 - child boundary must never print private configuration
        print(json.dumps({"write_readback": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
