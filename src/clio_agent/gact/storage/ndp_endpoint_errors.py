"""Bounded upstream diagnostics for NDP operations, without private bearer values."""

from __future__ import annotations

import json
import re
import time

import httpx

from clio_agent.gact.storage.ndp_endpoint_capture import capture_response
from clio_agent.gact.storage.ndp_endpoint_models import EndpointRequest


def raise_endpoint_error(
    response: httpx.Response,
    request: EndpointRequest,
    deadline: float,
    token: str | None = None,
) -> None:
    """Retain an actionable failure reason while bounding reads and redacting auth.

    A failed bridge start can wrap a downstream catalog or identity failure in
    HTTP 400. Dropping its body incorrectly makes that look like an invalid
    station selection. Redirects remain rejected without following them.
    """
    if response.status_code < 400:
        response.raise_for_status()
        return
    try:
        raw, _, _ = capture_response(response, request, min(deadline, time.monotonic() + 5))
    except (TimeoutError, httpx.HTTPError):
        raw = b""
    detail = ""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        value = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(value, dict):
            value = value.get("message")
        if isinstance(value, str):
            detail = value
        if not detail and isinstance(body.get("checks"), dict):
            failed_checks = []
            for name, check in body["checks"].items():
                if isinstance(check, dict) and check.get("status") not in {"up", "disabled"}:
                    failed_checks.append(
                        f"{name}: {check.get('error') or check.get('status') or 'unavailable'}"
                    )
            detail = "; ".join(failed_checks)
    elif raw and "text/plain" in response.headers.get("content-type", ""):
        detail = raw[:4096].decode("utf-8", errors="replace")
    if token:
        detail = detail.replace(token, "[redacted]")
    detail = re.sub(r"(?i)bearer\s+\S+|eyJ[A-Za-z0-9_.-]{40,}", "[redacted]", detail)
    detail = " ".join(detail.split())[:1000]
    message = f"Endpoint operation failed (HTTP {response.status_code})"
    if detail:
        message += ": " + detail
    if response.status_code in {401, 403}:
        hint = (
            "; use a connection authorized with the saved NDP account"
            if token is None
            else "; check this Endpoint's identity authority and access policy"
        )
        raise PermissionError(message + hint)
    raise ValueError(message)
