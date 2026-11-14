"""Construct the separate-origin MCP App iframe sandbox and CSP.

The helpers in this module validate trusted embedding origins, advertise a
distinct sandbox origin, construct the sandbox content-security policy, and
provide the outer iframe proxy document used by the GACT MCP Apps host.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from clio_agent.gact.mcp_app_sandbox_origin import sandbox_host_for
from clio_agent.gact.mcp_app_service_declaration import is_loopback_source
from clio_agent.gact.mcp_app_snapshot_script import SNAPSHOT_METHOD, SNAPSHOT_SCRIPT


def _host_origin(request: Request) -> str:
    """Validate and return the embedding origin: loopback, Tauri, or a trusted UI origin.

    A UI served from another name is trusted when its origin is a configured
    ``gact.cors.origins`` entry or its host is in ``gact.allowed_hosts`` (a CLIO
    reached over the network).
    """

    from clio_agent.gact.cors import gact_allowed_hosts, gact_cors_origins  # noqa: PLC0415

    referer = request.headers.get("referer", "")
    if not referer:
        raise HTTPException(status_code=403, detail="sandbox requires an embedding referrer")
    parsed = urlparse(referer)
    if parsed.scheme in {"tauri", "asset"}:
        return f"{parsed.scheme}:"
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hostname = (parsed.hostname or "").lower()
    allowed = gact_allowed_hosts()
    trusted = hostname in {"127.0.0.1", "localhost", "::1"} or (
        "*" not in allowed and hostname in allowed
    )
    if parsed.scheme not in {"http", "https"} or not (trusted or origin in gact_cors_origins()):
        raise HTTPException(status_code=403, detail="sandbox embedding origin is not allowed")
    return origin


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


def _sandbox_url(request: Request, path: str, *, namespace: str | None) -> str:
    """Advertise an absolute sandbox URL on the app's own sandbox host.

    The host is ``s<digest>.mcp-apps.localhost`` on this server's port, one per
    originating server (:mod:`clio_agent.gact.mcp_app_sandbox_origin`): never the
    web UI's or the API's origin, so the view may be same-origin with it.
    """

    port = request.url.port
    host = sandbox_host_for(namespace, (request.url.hostname or "localhost").lower())
    netloc = host + (f":{port}" if port else "")
    return f"{request.url.scheme}://{netloc}{path}"


def _safe_sources(value: Any) -> list[str]:
    """Drop CSP entries that could inject directives or reach loopback directly.

    A view reaches a live service only through CLIO's service route (MA23); the
    descriptor reports each refused loopback source as
    ``csp_loopback_source_refused``.
    """

    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, str)
        and item
        and not any(ch in item for ch in ";\r\n'\" ")
        and not is_loopback_source(item)
    ]


def _csp_header(
    csp: Mapping[str, Any], host_origin: str, service_sources: Sequence[str] = ()
) -> str:
    """Build the sandbox policy; ``service_sources`` are the app's own service route.

    Scripts may evaluate code (``'unsafe-eval'``, which also covers WebAssembly), as in
    the specification's reference host (``examples/basic-host``): a CesiumJS globe, a
    WebAssembly decoder or a chemistry editor need it, and the view runs on its own
    sandbox host, which carries no API authority (apps ledger MC-D1).
    """

    # CLIO's own route on the sandbox origin: built by CLIO, never declared by the view.
    service = " ".join(service_sources)
    resources = " ".join(_safe_sources(csp.get("resourceDomains")))
    connects = " ".join([*_safe_sources(csp.get("connectDomains")), service]).strip()
    frames = " ".join([*_safe_sources(csp.get("frameDomains")), service]).strip()
    bases = " ".join(_safe_sources(csp.get("baseUriDomains")))
    return "; ".join(
        [
            "default-src 'self' 'unsafe-inline'",
            f"script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: {resources}".strip(),
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


_SANDBOX_TEMPLATE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<title>MCP App sandbox</title><style>
html,body{margin:0;width:100%;height:100%;background:transparent}body{display:flex}
iframe{border:0;flex:1;width:100%;height:100%;background:transparent}
</style></head><body><script>@@SNAPSHOT_SCRIPT@@</script><script>
(() => {
  if (window.self === window.top || !document.referrer) throw new Error('invalid sandbox embed');
  const expected = new URL(document.referrer).origin;
  const parentTarget = expected === 'null' ? '*' : expected;
  try { window.top.document; throw new Error('sandbox isolation failed'); } catch (error) {
    if (error instanceof Error && error.message === 'sandbox isolation failed') throw error;
  }
  const inner = document.createElement('iframe');
  // The view is same-origin with this sandbox host, which carries no API authority.
  const base = 'allow-scripts allow-same-origin allow-forms';
  inner.setAttribute('sandbox', base);
  document.body.appendChild(inner);
  window.addEventListener('message', (event) => {
    if (event.source === window.parent) {
      if (event.origin !== expected) return;
      if (event.data?.method === 'ui/notifications/sandbox-resource-ready') {
        const { html, sandbox, permissions } = event.data.params || {};
        if (typeof sandbox === 'string') {
          const allowed = new Set(['allow-scripts', 'allow-same-origin', 'allow-forms',
            'allow-modals', 'allow-popups', 'allow-downloads', 'allow-pointer-lock']);
          const tokens = sandbox.split(/\s+/).filter(token => allowed.has(token));
          inner.setAttribute('sandbox', tokens.join(' ') || base);
        }
        const allow = [];
        if (permissions?.camera) allow.push('camera');
        if (permissions?.microphone) allow.push('microphone');
        if (permissions?.geolocation) allow.push('geolocation');
        if (permissions?.clipboardWrite) allow.push('clipboard-write');
        if (allow.length) inner.setAttribute('allow', allow.join('; '));
        if (typeof html !== 'string') return;
        inner.srcdoc = html;
      } else if (event.data?.method === '@@SNAPSHOT_METHOD@@' && event.data.id !== undefined) {
        // The host asks for a snapshot of the view: answered here, never sent to the view.
        const id = event.data.id;
        const reply = (result) => window.parent.postMessage({jsonrpc: '2.0', id, result}, parentTarget);
        const frame = () => new Promise((done) => requestAnimationFrame(() => done()));
        // The view frame is a flex item: max-width narrows it (width alone does not), and a
        // view with a minimum height may overflow, so both sides move by one pixel.
        const nudge = async () => {
          inner.style.maxWidth = 'calc(100% - 1px)';
          inner.style.height = 'calc(100% - 1px)';
          await frame(); await frame();
          inner.style.maxWidth = '';
          inner.style.height = '';
        };
        let view = null;
        try { view = inner.contentWindow; void view.document.documentElement; } catch { view = null; }
        if (!view) {
          reply({error: {reason: 'view_not_readable', message: 'The view runs on another origin, so CLIO cannot read its pixels.'}});
          return;
        }
        clioViewSnapshot(view, {...(event.data.params || {}), nudge}).then(reply, (error) =>
          reply({error: {reason: 'snapshot_failed', message: String(error && error.message || error)}}));
      } else {
        inner.contentWindow?.postMessage(event.data, '*');
      }
    } else if (event.source === inner.contentWindow
        && (event.origin === 'null' || event.origin === window.location.origin)) {
      window.parent.postMessage(event.data, parentTarget);
    }
  });
  window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/sandbox-proxy-ready',params:{}}, parentTarget);
})();
</script></body></html>"""

#: The outer sandbox document: the frame proxy plus the view snapshot (CM1).
_SANDBOX_DOCUMENT = _SANDBOX_TEMPLATE.replace("@@SNAPSHOT_SCRIPT@@", SNAPSHOT_SCRIPT).replace(
    "@@SNAPSHOT_METHOD@@", SNAPSHOT_METHOD
)


__all__ = [
    "_SANDBOX_DOCUMENT",
    "_csp_header",
    "_host_origin",
    "_request_origin",
    "_safe_sources",
    "_sandbox_url",
]
