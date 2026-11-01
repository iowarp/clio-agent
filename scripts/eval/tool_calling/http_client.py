"""A small JSON client for the GACT REST API (standard library only).

A loopback peer needs no token. A remote server needs the bearer token the
server was started with (``CLIO_GACT_BEARER_TOKEN``); pass it with
``--token-env``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class GactError(RuntimeError):
    """A non-2xx answer, with the typed error body when there is one."""

    def __init__(self, method: str, path: str, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}: {json.dumps(body)[:400]}")


def _starting(body: Any) -> bool:
    """A typed, recoverable refusal from services still starting (``task_runtime_starting``)."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        error = body["detail"].get("error")
    if not isinstance(error, dict):
        return False
    return bool(error.get("recoverable")) and str(error.get("error") or "").endswith("_starting")


def urllib_transport(
    method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class GactClient:
    """JSON calls against one server."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 120.0,
        transport: Transport = urllib_transport,
        starting_wait_s: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._transport = transport
        self.starting_wait_s = starting_wait_s
        self._sleep = sleep

    def _call(
        self, method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float
    ) -> tuple[int, Any]:
        status, raw = self._transport(method, url, headers, data, timeout)
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self.base_url + path + (f"?{urlencode(query, doseq=True)}" if query else "")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        waited = 0.0
        while True:
            status, parsed = self._call(method, url, headers, data, timeout or self.timeout)
            if 200 <= status < 300:
                return parsed
            if status != 503 or not _starting(parsed) or waited >= self.starting_wait_s:
                raise GactError(method, path, status, parsed)
            # A booting server refuses with a recoverable "...starting" error.
            self._sleep(2.0)
            waited += 2.0

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, query=query or None)

    def post(self, path: str, body: Any = None, *, timeout: float | None = None) -> Any:
        return self.request("POST", path, {} if body is None else body, timeout=timeout)

    # ----------------------------------------------------------------- routes

    def health(self) -> Any:
        return self.get("/v1/health")

    def fleet(self) -> dict[str, Any]:
        return self.get("/v1/fleet")

    def fleet_settings(self) -> dict[str, Any]:
        return self.get("/v1/settings/fleet")

    def workspaces(self) -> list[dict[str, Any]]:
        return list((self.get("/v1/workspaces") or {}).get("workspaces") or [])

    def create_workspace(self, name: str, root_path: str) -> dict[str, Any]:
        return self.post("/v1/workspaces", {"name": name, "root_path": root_path})

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/v1/sessions", body)

    def session(self, sid: str) -> dict[str, Any]:
        return self.get(f"/v1/sessions/{quote(sid)}")

    def messages(self, sid: str, limit: int = 5) -> list[dict[str, Any]]:
        return list(
            (self.get(f"/v1/sessions/{quote(sid)}/messages", limit=limit) or {}).get("messages")
            or []
        )

    def send(self, sid: str, text: str) -> dict[str, Any]:
        return self.post(
            f"/v1/sessions/{quote(sid)}/messages",
            {"parts": [{"type": "text", "text": text}], "delivery": "start"},
        )

    def cancel(self, sid: str) -> Any:
        return self.post(f"/v1/sessions/{quote(sid)}/cancel")

    def export(self, sid: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{quote(sid)}/export", timeout=600.0)

    def marketplaces(self) -> list[dict[str, Any]]:
        return list((self.get("/v1/plugins/marketplaces") or {}).get("marketplaces") or [])

    def update_marketplace(self, name: str) -> Any:
        return self.post(f"/v1/plugins/marketplaces/{quote(name)}/update")

    def install_marketplace(self, source: str, name: str) -> Any:
        return self.post("/v1/plugins/marketplaces", {"source": source, "name": name})

    def trust_marketplace(self, name: str) -> Any:
        return self.post(f"/v1/plugins/marketplaces/{quote(name)}/trust")

    def resolve_agent(self, ref: str, add: list[str]) -> Any:
        return self.get("/v1/agents/resolve", ref=ref, add=add)
