"""Small REST helpers for the local CLIO server the demo runs against (loopback, no token)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("CLIO_URL", "http://127.0.0.1:8787")


def call(method: str, path: str, body: Any = None, timeout: float = 120) -> Any:
    """One JSON request; an HTTP error is raised with its status and body."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:600]
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from None


def workspace(name: str, root: str) -> dict[str, Any]:
    """The workspace rooted at ``root``, created when there is none."""
    for row in call("GET", "/v1/workspaces")["workspaces"]:
        if row.get("root_path") == root:
            return row
    return call("POST", "/v1/workspaces", {"name": name, "root_path": root})


def sources(workspace_id: str) -> list[dict[str, Any]]:
    """The workspace's connected data sources."""
    body = call("GET", f"/v1/workspaces/{workspace_id}/sources")
    return body.get("sources", body) if isinstance(body, dict) else body
