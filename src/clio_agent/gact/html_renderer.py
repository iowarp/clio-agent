"""A public, stateless renderer bootstrap; document bytes stay in the browser."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.types import Scope

RENDERER_PATH = "/v1/html-renderer"
LIBRARY_ORIGINS = "https://cdn.jsdelivr.net https://unpkg.com https://esm.sh https://cdnjs.cloudflare.com https://cdn.babylonjs.com"
RENDERER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' 'wasm-unsafe-eval' blob: "
    + LIBRARY_ORIGINS
    + "; style-src 'unsafe-inline' "
    + LIBRARY_ORIGINS
    + "; img-src data: blob: https:; font-src data: "
    + LIBRARY_ORIGINS
    + "; media-src data: blob:; worker-src blob:; connect-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
RENDERER_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<script>
addEventListener('message', function receive(event) {
  if (event.source !== parent || event.data?.type !== 'clio.html-preview' ||
      typeof event.data.html !== 'string') return;
  removeEventListener('message', receive);
  parent.postMessage({type: 'clio.html-preview.ready'}, '*');
  document.open(); document.write(event.data.html); document.close();
});
</script></body></html>"""


def is_public_html_renderer(scope: Scope) -> bool:
    """Admit only the fixed GET document, never an API or user-authored resource."""
    return (
        scope["type"] == "http"
        and scope.get("method") == "GET"
        and scope.get("path") == RENDERER_PATH
    )


def register_html_renderer(app: FastAPI) -> None:
    """Serve an independent CSP document for Desktop's opaque preview iframe."""

    @app.get(RENDERER_PATH, include_in_schema=False, response_class=HTMLResponse)
    def renderer() -> HTMLResponse:
        return HTMLResponse(
            RENDERER_HTML,
            headers={
                "Content-Security-Policy": RENDERER_CSP,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
