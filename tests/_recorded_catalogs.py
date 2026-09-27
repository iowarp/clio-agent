"""The online model catalogs, replayed from recordings on a loopback server.

WHY. Every :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog` fetches a
remote document on first use: CLIO's own catalogs from raw GitHub, models.dev, and
LiteLLM's community cost map. Dozens of unit tests reach one of them through the
capability cascade or a catalog refresh, which made them network-dependent (and the
network guard, tests/_network_guard.py, now fails any test that tries).

WHAT. :func:`install` starts a loopback HTTP server that serves a recording of each
catalog, and routes ``fetched_catalog``'s fetches for those URLs to it. The recordings:

* CLIO's catalogs (``catalogs/*.json`` on raw GitHub): this checkout's own
  ``catalogs/`` files, which is exactly what those URLs serve for the tree under test;
* LiteLLM's cost map: the copy LiteLLM ships with its package (version-locked with the
  installed LiteLLM);
* models.dev: ``tests/fixtures/recorded_catalogs/models-dev.json.gz``, recorded
  2026-09-26 from https://models.dev/models.json;
* Hugging Face repo metadata/files: ``tests/fixtures/recorded_catalogs/huggingface/``
  mirrors the URL path (recorded 2026-09-26). A repo with no recording replays as a
  404, the answer the real API gives a repo it does not know.

Only the fetch URL is rewritten: ``source_url`` and every cache record keep the real
one. A test that installs its own fake ``httpx.get`` on ``fetched_catalog.httpx`` (the
catalog unit tests do) sees the real URL and its fake wins.
"""

from __future__ import annotations

import gzip
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_RECORDINGS = _REPO / "tests" / "fixtures" / "recorded_catalogs"
_MODELS_DEV_RECORDING = _RECORDINGS / "models-dev.json.gz"
_HF_PREFIX = "https://huggingface.co/"

_server: ThreadingHTTPServer | None = None
_routes: dict[str, Callable[[], bytes]] = {}


def _litellm_cost_map() -> bytes:
    import litellm  # noqa: PLC0415

    return (Path(litellm.__file__).parent / "model_prices_and_context_window_backup.json").read_bytes()


def _recordings() -> dict[str, Callable[[], bytes]]:
    """Real catalog URL -> loader of its recorded body."""
    from clio_agent.providers.fetched_catalog import CLIO_CATALOG_BASE_URL  # noqa: PLC0415
    from clio_agent.providers.handshake.sources import litellm_catalog, models_dev  # noqa: PLC0415

    routes: dict[str, Callable[[], bytes]] = {
        models_dev.MODELS_DEV_URL: lambda: gzip.decompress(_MODELS_DEV_RECORDING.read_bytes()),
        litellm_catalog._cost_map_url(): _litellm_cost_map,
        litellm_catalog._DEFAULT_COST_MAP_URL: _litellm_cost_map,
    }
    for path in sorted((_REPO / "catalogs").glob("*.json")):
        routes[f"{CLIO_CATALOG_BASE_URL}/{path.name}"] = path.read_bytes
    return routes


def _huggingface_recording(url: str) -> bytes | None:
    root = (_RECORDINGS / "huggingface").resolve()
    target = (root / url.removeprefix(_HF_PREFIX)).resolve()
    if root not in target.parents or not target.is_file():
        return None
    return target.read_bytes()


def _is_recorded(url: str) -> bool:
    return url in _routes or url.startswith(_HF_PREFIX)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        url = self.path.lstrip("/")
        loader = _routes.get(url)
        body = loader() if loader is not None else _huggingface_recording(url)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass


class _HttpxRouter:
    """Stands in for ``fetched_catalog.httpx``: recorded URLs go to the loopback server."""

    def __init__(self, real: Any, base: str) -> None:
        self._real = real
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)  # HTTPError, Response, ...

    def get(self, url: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(url)
        if _is_recorded(text):
            url = f"{self._base}/{text}"
        return self._real.get(url, *args, **kwargs)


def install() -> None:
    """Start the replay server and route the catalog fetches to it (idempotent)."""
    global _server
    if _server is not None:
        return
    from clio_agent.providers import fetched_catalog  # noqa: PLC0415

    _routes.update(_recordings())
    _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=_server.serve_forever, name="recorded-catalogs", daemon=True).start()
    fetched_catalog.httpx = _HttpxRouter(  # type: ignore[assignment]
        fetched_catalog.httpx, f"http://127.0.0.1:{_server.server_port}"
    )


def uninstall() -> None:
    """Stop the replay server (the suite's ``pytest_unconfigure``)."""
    global _server
    if _server is None:
        return
    from clio_agent.providers import fetched_catalog  # noqa: PLC0415

    if isinstance(fetched_catalog.httpx, _HttpxRouter):
        fetched_catalog.httpx = fetched_catalog.httpx._real  # type: ignore[assignment]
    _server.shutdown()
    _server.server_close()
    _server = None
