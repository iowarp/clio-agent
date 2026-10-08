"""Collect actual asynchronous indexing through the public storage API in focused tests."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from httpx import Response


def complete_indexing(client: TestClient, prefix: str, accepted: Response) -> None:
    """Require acceptance, then successful owner settlement before checking linked content."""
    assert accepted.status_code == 202, accepted.text
    operation = accepted.json()["indexing_operation"]
    assert operation["kind"] == "indexing"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(prefix + "/operations")
        assert response.status_code == 200, response.text
        current = next(row for row in response.json()["operations"] if row["id"] == operation["id"])
        if current["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            assert current["state"] == "completed", current
            assert current["manifest_id"]
            return
        time.sleep(0.01)
    raise AssertionError("The accepted indexing owner did not settle")
