"""Construct the separate-origin MCP App iframe sandbox and CSP.

The helpers in this module validate trusted embedding origins, advertise a
distinct sandbox origin, construct the sandbox content-security policy, and
provide the outer iframe proxy document used by the GACT MCP Apps host.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request


def _host_origin(request: Request) -> str:
    """Validate and return the loopback/Tauri embedding origin."""

    referer = request.headers.get("referer", "")
    if not referer:
        raise HTTPException(status_code=403, detail="sandbox requires an embedding referrer")
    parsed = urlparse(referer)
    if parsed.scheme in {"tauri", "asset"}:
        return f"{parsed.scheme}:"
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise HTTPException(status_code=403, detail="sandbox embedding origin is not allowed")
    return f"{parsed.scheme}://{parsed.netloc}"


def _request_origin(value: str) -> str:
    """Return the serialized origin for an Origin/Referer value."""

    if value == "null":
        return value
    parsed = urlparse(value)
    if not parsed.scheme:
        return ""
    if parsed.scheme in {"tauri", "asset"}:
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""


def _alternate_loopback_origin(origin: str) -> str:
    """Return the same listener through a distinct loopback web origin."""

    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    if hostname == "127.0.0.1":
        alternate = "localhost"
    elif hostname in {"localhost", "::1"}:
        alternate = "127.0.0.1"
    else:
        return origin
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{alternate}{port}"


def _sandbox_url(request: Request, path: str) -> str:
    """Advertise an absolute sandbox URL on a distinct host origin.

    The packaged web client and GACT API share an origin. In that case the
    same loopback listener is addressed through the other canonical loopback
    hostname (``127.0.0.1`` versus ``localhost``). Development web servers and
    Tauri already differ from the HTTP backend, so they keep the backend
    origin. The web host still rejects any accidental same-origin result.
    """

    backend = f"{request.url.scheme}://{request.url.netloc}"
    embedding = _request_origin(request.headers.get("origin", ""))
    if not embedding:
        embedding = _request_origin(request.headers.get("referer", ""))
    sandbox_origin = backend
    if not embedding or embedding == backend:
        sandbox_origin = _alternate_loopback_origin(backend)
    return f"{sandbox_origin}{path}"


def _safe_sources(value: Any) -> list[str]:
    """Drop CSP entries that could inject additional directives."""

    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and item and not any(ch in item for ch in ";\r\n'\" ")
    ]


def _csp_header(csp: Mapping[str, Any], host_origin: str) -> str:
    resources = " ".join(_safe_sources(csp.get("resourceDomains")))
    connects = " ".join(_safe_sources(csp.get("connectDomains")))
    frames = " ".join(_safe_sources(csp.get("frameDomains")))
    bases = " ".join(_safe_sources(csp.get("baseUriDomains")))
    return "; ".join(
        [
            "default-src 'self' 'unsafe-inline'",
            f"script-src 'self' 'unsafe-inline' blob: data: {resources}".strip(),
            f"style-src 'self' 'unsafe-inline' blob: data: {resources}".strip(),
            f"img-src 'self' data: blob: {resources}".strip(),
            f"font-src 'self' data: blob: {resources}".strip(),
            f"media-src 'self' data: blob: {resources}".strip(),
            f"connect-src 'self' {connects}".strip(),
            f"worker-src 'self' blob: {resources}".strip(),
            f"frame-src 'self' data: blob: {frames}".strip(),
            f"base-uri {bases}" if bases else "base-uri 'none'",
            "object-src 'none'",
            f"frame-ancestors {host_origin}",
        ]
    )


_SANDBOX_DOCUMENT = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<title>MCP App sandbox</title><style>
html,body{margin:0;width:100%;height:100%;background:transparent}body{display:flex}
iframe{border:0;flex:1;width:100%;height:100%;background:transparent}
</style></head><body><script>
(() => {
  if (window.self === window.top || !document.referrer) throw new Error('invalid sandbox embed');
  const expected = new URL(document.referrer).origin;
  const parentTarget = expected === 'null' ? '*' : expected;
  try { window.top.document; throw new Error('sandbox isolation failed'); } catch (error) {
    if (error instanceof Error && error.message === 'sandbox isolation failed') throw error;
  }
  const inner = document.createElement('iframe');
  inner.setAttribute('sandbox', 'allow-scripts allow-forms');
  document.body.appendChild(inner);
  window.addEventListener('message', (event) => {
    if (event.source === window.parent) {
      if (event.origin !== expected) return;
      if (event.data?.method === 'ui/notifications/sandbox-resource-ready') {
        const { html, sandbox, permissions } = event.data.params || {};
        if (typeof sandbox === 'string') {
          const allowed = new Set(['allow-scripts', 'allow-forms', 'allow-modals',
            'allow-popups', 'allow-downloads', 'allow-pointer-lock']);
          const tokens = sandbox.split(/\s+/).filter(token => allowed.has(token));
          inner.setAttribute('sandbox', tokens.join(' ') || 'allow-scripts allow-forms');
        }
        const allow = [];
        if (permissions?.camera) allow.push('camera');
        if (permissions?.microphone) allow.push('microphone');
        if (permissions?.geolocation) allow.push('geolocation');
        if (permissions?.clipboardWrite) allow.push('clipboard-write');
        if (allow.length) inner.setAttribute('allow', allow.join('; '));
        if (typeof html !== 'string') return;
        inner.srcdoc = html;
      } else {
        inner.contentWindow?.postMessage(event.data, '*');
      }
    } else if (event.source === inner.contentWindow && event.origin === 'null') {
      window.parent.postMessage(event.data, parentTarget);
    }
  });
  window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/sandbox-proxy-ready',params:{}}, parentTarget);
})();
</script></body></html>"""


__all__ = [
    "_SANDBOX_DOCUMENT",
    "_alternate_loopback_origin",
    "_csp_header",
    "_host_origin",
    "_request_origin",
    "_safe_sources",
    "_sandbox_url",
]
