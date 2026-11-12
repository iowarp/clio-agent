"""HTTP plumbing shared by every search source: the client, typed failures, MCP calls.

Sources are hosted HTTP APIs or hosted MCP servers (streamable HTTP). Failures are
classified from the HTTP status and structured fields only -- never from a
source's prose -- into a :class:`SourceError` whose ``kind`` drives rotation.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

#: Identifies CLIO honestly to every source (some require an app name).
USER_AGENT = "clio-agent (+https://github.com/iowarp/clio-agent)"
APP_TITLE = "CLIO"

#: Failure kinds: what a source's answer means for the next search.
RATE_LIMITED = "rate_limited"  # too many requests now; retry after a short wait
ALLOWANCE_USED = "allowance_used"  # free allowance used up until it resets
KEY_REJECTED = "key_rejected"  # the person's key was refused
FAILED = "failed"  # network, server error or an answer CLIO cannot read

#: Cooldown when a source gives no Retry-After.
DEFAULT_COOLDOWN_S = {RATE_LIMITED: 60.0, ALLOWANCE_USED: 3600.0, KEY_REJECTED: 0.0, FAILED: 30.0}
#: A Retry-After longer than this means the allowance is used up, not a burst limit.
ALLOWANCE_THRESHOLD_S = 900.0


@dataclass
class SourceError(Exception):
    """A source could not answer; ``kind`` is one of the failure kinds above."""

    source: str
    kind: str
    detail: str = ""
    retry_after_s: float | None = None

    def __str__(self) -> str:
        return f"{self.source}: {self.kind} {self.detail}".strip()

    def cooldown_s(self) -> float:
        """How long the source should rest before CLIO tries it again."""

        if self.retry_after_s is not None and self.retry_after_s > 0:
            return self.retry_after_s
        return DEFAULT_COOLDOWN_S[self.kind]


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or an HTTP date) into seconds."""

    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def classify(source: str, response: httpx.Response, *, keyed: bool) -> SourceError | None:
    """The typed failure an HTTP answer means, or ``None`` when it succeeded."""

    status = response.status_code
    if 200 <= status < 300:
        return None
    wait = retry_after_seconds(response.headers.get("retry-after"))
    detail = f"HTTP {status}"
    if status == 429:
        kind = ALLOWANCE_USED if wait is not None and wait > ALLOWANCE_THRESHOLD_S else RATE_LIMITED
        return SourceError(source, kind, detail, wait)
    if status == 402:
        return SourceError(source, ALLOWANCE_USED, detail, wait)
    if status in (401, 403) and keyed:
        return SourceError(source, KEY_REJECTED, detail)
    return SourceError(source, FAILED, detail, wait)


#: One client per event loop: an httpx connection pool belongs to the loop that made it.
_CLIENTS: dict[int, httpx.AsyncClient] = {}
_CLIENT_LOCK = threading.Lock()


def client() -> httpx.AsyncClient:
    """The async HTTP client for the running event loop (one pool for every source)."""

    loop_id = id(asyncio.get_running_loop())
    with _CLIENT_LOCK:
        current = _CLIENTS.get(loop_id)
        if current is None or current.is_closed:
            current = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=httpx.Timeout(20.0, connect=10.0),
            )
            _CLIENTS[loop_id] = current
        return current


async def request(
    source: str,
    method: str,
    url: str,
    *,
    keyed: bool = False,
    timeout: float = 20.0,
    **kwargs: Any,
) -> httpx.Response:
    """One HTTP request; any failure becomes a :class:`SourceError`."""

    try:
        response = await client().request(method, url, timeout=timeout, **kwargs)
    except httpx.HTTPError as exc:
        raise SourceError(source, FAILED, f"{type(exc).__name__}: {exc}"[:300]) from exc
    failure = classify(source, response, keyed=keyed)
    if failure is not None:
        raise failure
    return response


def json_body(source: str, response: httpx.Response) -> Any:
    """The JSON body, or a typed failure when it is not JSON."""

    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(source, FAILED, "answer is not JSON") from exc


# --------------------------------------------------------------------- MCP calls

_MCP_ACCEPT = "application/json, text/event-stream"
_MCP_PROTOCOL = "2025-06-18"
_SESSIONS: dict[str, str] = {}
_SESSIONS_LOCK = threading.Lock()


def _rpc_payload(source: str, response: httpx.Response) -> dict[str, Any]:
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        data = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
        if not data:
            raise SourceError(source, FAILED, "empty event stream")
        text = data[-1]
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SourceError(source, FAILED, "MCP answer is not JSON") from exc
    if not isinstance(payload, dict):
        raise SourceError(source, FAILED, "MCP answer is not an object")
    return payload


async def _mcp_post(
    source: str, url: str, body: dict[str, Any], headers: dict[str, str], *, keyed: bool
) -> httpx.Response:
    return await request(
        source,
        "POST",
        url,
        keyed=keyed,
        json=body,
        headers={"accept": _MCP_ACCEPT, "content-type": "application/json", **headers},
    )


async def _mcp_session(source: str, url: str, headers: dict[str, str], *, keyed: bool) -> str:
    with _SESSIONS_LOCK:
        known = _SESSIONS.get(url)
    if known is not None:
        return known
    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "clio-agent", "version": "1"},
        },
    }
    response = await _mcp_post(source, url, init, headers, keyed=keyed)
    session = response.headers.get("mcp-session-id", "")
    extra = {"mcp-session-id": session} if session else {}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    await _mcp_post(source, url, note, {**headers, **extra}, keyed=keyed)
    with _SESSIONS_LOCK:
        _SESSIONS[url] = session
    return session


async def mcp_tool(
    source: str,
    url: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    keyed: bool = False,
) -> dict[str, Any]:
    """Call ``tool`` on a hosted MCP server and return its ``result`` object.

    The session is opened once per server URL and reopened once if the server
    forgot it. A JSON-RPC error or ``isError`` result is a typed failure.
    """

    base = dict(headers or {})
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
    body["params"] = {"name": tool, "arguments": arguments}
    for attempt in (1, 2):
        session = await _mcp_session(source, url, base, keyed=keyed)
        extra = {"mcp-session-id": session} if session else {}
        try:
            response = await _mcp_post(source, url, body, {**base, **extra}, keyed=keyed)
        except SourceError as exc:
            if attempt == 1 and exc.kind == FAILED and exc.detail in ("HTTP 400", "HTTP 404"):
                with _SESSIONS_LOCK:
                    _SESSIONS.pop(url, None)
                continue
            raise
        payload = _rpc_payload(source, response)
        if "error" in payload:
            error = payload["error"] if isinstance(payload["error"], dict) else {}
            code = error.get("code")
            # Hosted free tiers answer a used-up allowance as JSON-RPC -32000
            # (Parallel even with HTTP 200, no Retry-After): rest the source.
            kind = ALLOWANCE_USED if code == -32000 else FAILED
            raise SourceError(source, kind, f"MCP error {code}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SourceError(source, FAILED, "MCP result missing")
        if result.get("isError"):
            raise SourceError(source, FAILED, "tool reported an error")
        return result
    raise SourceError(source, FAILED, "MCP session could not be reopened")


def mcp_text(result: dict[str, Any]) -> str:
    """The concatenated text content of an MCP tool result."""

    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(row.get("text", ""))
        for row in content
        if isinstance(row, dict) and row.get("type") == "text"
    )


def reset_for_tests() -> None:
    """Forget MCP sessions and the shared client (tests only)."""

    with _SESSIONS_LOCK:
        _SESSIONS.clear()
    with _CLIENT_LOCK:
        _CLIENTS.clear()


__all__ = [
    "ALLOWANCE_USED",
    "APP_TITLE",
    "FAILED",
    "KEY_REJECTED",
    "RATE_LIMITED",
    "SourceError",
    "classify",
    "client",
    "json_body",
    "mcp_text",
    "mcp_tool",
    "request",
    "reset_for_tests",
    "retry_after_seconds",
]
