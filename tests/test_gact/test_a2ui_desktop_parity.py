"""S8 deliverable 6 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): desktop parity, server side.

The client-side architecture (owner decision 7, campaign doc:
``external/gact-tui/packages/core/src/v3/a2ui/client-metadata.ts``) already
states the invariant: "the transport never sees or builds this metadata, it
only carries whatever the repository layer merges in — identical by
construction," so browser (fetch) and Tauri (desktop webview IPC bridged
through Rust) send the EXACT SAME ``a2uiClientCapabilities``/
``a2uiClientDataModel`` JSON body.

Real recorded request metadata, not hand-invented headers (S8 review fix):
``tests/fixtures/a2ui_client_metadata/`` vendors the literal header sets
BOTH gact-tui transports construct --
``external/gact-tui/web/src/lib/transport/browser-transport.ts`` and
``tauri-transport.ts``'s own private ``headers()`` methods are
byte-for-byte identical (``Accept``/``Content-Type``/``X-GACT-Version``/
``X-A2UI-Version``/optional ``Authorization``) -- and the
``a2uiClientCapabilities`` body shape + concrete catalog ids
``external/gact-tui/web/src/lib/a2ui/registry-store.test.tsx`` (S6
adversarial review) asserts the real client advertises. No Tauri-origin
network recording exists in the gact-tui repo to vendor verbatim (the
desktop e2e suite drives a real WebView rather than capturing raw HTTP), so
this module's Tauri fixture is derived from the real Rust bridge source
instead of invented: ``desktop/src-tauri/src/gact_http.rs``'s own module
doc states the WebView origin (``http://tauri.localhost``) is cross-origin
to the local sidecar and clio emits no ``Access-Control-Allow-Origin``, so
a vanilla browser ``fetch()`` would be CORS-blocked -- ``gact_http`` is a
Tauri command that performs the request from Rust with the ``ureq`` native
HTTP client instead, which (unlike a browser engine) never auto-attaches an
``Origin`` header. This module's SERVER-side half of that proof:
``src/clio_agent/gact/cors.py``'s origin allowlist (``_DEFAULT_ORIGINS``)
is the only place a request's ``Origin`` is EVER consulted server-side, and
it exists purely to gate browser CORS preflight -- the actual metadata
door (``a2ui_capabilities.apply_client_metadata_guards``) never reads
``Origin`` or any other transport-identifying header. Both vendored
fixture requests must be remembered byte-for-byte identically.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_capabilities import (
    A2UI_CLIENT_CAPABILITIES_METADATA_KEY,
    A2UI_CLIENT_DATA_MODEL_METADATA_KEY,
    client_capabilities,
)
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.app import build_app

pytestmark = pytest.mark.usefixtures("host_agent_executor")

BASIC_ID = basic_catalog_id()
WORKSPACE_ID = workspace_catalog_id()

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_client_metadata"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


_BROWSER_FIXTURE = _load_fixture("browser_request.json")
_DESKTOP_FIXTURE = _load_fixture("desktop_request.json")
_CAPABILITIES_FIXTURE = _load_fixture("client_capabilities_body.json")

#: The literal header set each real gact-tui transport constructs (vendored,
#: not hand-written) -- see module docstring. Note what is ABSENT: neither
#: fixture's own JS-authored ``headers`` dict carries ``Origin`` -- a real
#: browser request gets one anyway, auto-attached by the browser engine
#: itself (never by gact-tui's own code); a real Tauri request never does.
BROWSER_HEADERS: dict[str, str] = {
    **_BROWSER_FIXTURE["headers"],
    "Origin": _BROWSER_FIXTURE["origin"],
}
DESKTOP_HEADERS: dict[str, str] = dict(_DESKTOP_FIXTURE["headers"])
assert "Origin" not in DESKTOP_HEADERS, "a real Tauri request never carries Origin"

#: The exact a2uiClientCapabilities body shape + concrete catalog ids
#: real gact-tui client code advertises (vendored, see module docstring).
CLIENT_CAPABILITIES_BODY: dict[str, Any] = _CAPABILITIES_FIXTURE["a2uiClientCapabilities"]
# Sanity: the vendored fixture's catalog ids are still today's real builtin
# ids -- if a builtin catalog id ever changes, this fixture is stale and
# this assertion (not a mismatched test failure three lines down) says so.
assert CLIENT_CAPABILITIES_BODY["v0.9"]["supportedCatalogIds"] == [WORKSPACE_ID, BASIC_ID]


class _FakeClioAgent:
    """Minimal stand-in for ClioAgent (no LM needed) -- mirrors
    test_a2ui_capabilities.py's own fixture so POST /messages succeeds."""

    def forward(self, question: str, session_id: str) -> Any:
        return SimpleNamespace(
            answer="ok",
            selected_expert="",
            routing_rationale="",
            route_source="",
            route_reason="",
            error_info=None,
        )


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeClioAgent())
    ) as test_client:
        yield test_client


def _create_session(client: TestClient) -> str:
    return client.post("/v1/sessions", json={"title": "parity"}).json()["id"]


def _post_capabilities(client: TestClient, sid: str, headers: dict[str, str]) -> Any:
    return client.post(
        f"/v1/sessions/{sid}/messages",
        headers=headers,
        json={
            "parts": [{"type": "text", "text": "hi"}],
            "metadata": {"a2uiClientCapabilities": CLIENT_CAPABILITIES_BODY},
        },
    )


def test_browser_and_desktop_requests_remember_identical_capabilities(
    client: TestClient,
) -> None:
    sid_browser = _create_session(client)
    sid_desktop = _create_session(client)

    browser_resp = _post_capabilities(client, sid_browser, BROWSER_HEADERS)
    desktop_resp = _post_capabilities(client, sid_desktop, DESKTOP_HEADERS)

    assert browser_resp.status_code == 200, browser_resp.text
    assert desktop_resp.status_code == 200, desktop_resp.text

    browser_stored = client.app.state.sessions.get(sid_browser).metadata[
        A2UI_CLIENT_CAPABILITIES_METADATA_KEY
    ]
    desktop_stored = client.app.state.sessions.get(sid_desktop).metadata[
        A2UI_CLIENT_CAPABILITIES_METADATA_KEY
    ]
    assert browser_stored == desktop_stored == CLIENT_CAPABILITIES_BODY

    browser_caps = client_capabilities(client.app, sid_browser)
    desktop_caps = client_capabilities(client.app, sid_desktop)
    assert browser_caps is not None
    assert desktop_caps is not None
    assert browser_caps.model_dump(mode="json") == desktop_caps.model_dump(mode="json")


def test_desktop_request_with_no_origin_header_is_not_treated_as_untrusted(
    client: TestClient,
) -> None:
    """A real Tauri request never carries ``Origin`` at all (module
    docstring) -- the metadata door must not silently degrade or refuse a
    request just because that header is absent (⚑ no-silent-fallback)."""

    sid = _create_session(client)
    assert "Origin" not in DESKTOP_HEADERS

    response = _post_capabilities(client, sid, DESKTOP_HEADERS)

    assert response.status_code == 200, response.text
    caps = client_capabilities(client.app, sid)
    assert caps is not None
    assert list(caps.v0_9.supportedCatalogIds) == [WORKSPACE_ID, BASIC_ID]


def test_data_model_metadata_also_parity_across_transports(client: TestClient) -> None:
    """The SAME parity claim extends to ``a2uiClientDataModel`` (S3), the
    second official transport-metadata object the client attaches."""

    sid_browser = _create_session(client)
    sid_desktop = _create_session(client)
    stored_data_models: dict[str, Any] = {}
    for sid, headers in ((sid_browser, BROWSER_HEADERS), (sid_desktop, DESKTOP_HEADERS)):
        created = client.post(
            f"/v1/sessions/{sid}/a2ui/messages",
            headers={**headers, "X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"},
            json={
                "messages": [
                    {
                        "version": "v0.9.1",
                        "createSurface": {
                            "surfaceId": "surface_1",
                            "catalogId": WORKSPACE_ID,
                            "sendDataModel": True,
                        },
                    }
                ]
            },
        )
        assert created.status_code == 200, created.text

        posted = client.post(
            f"/v1/sessions/{sid}/messages",
            headers=headers,
            json={
                "parts": [{"type": "text", "text": "hi"}],
                "metadata": {
                    "a2uiClientDataModel": {
                        "version": "v0.9.1",
                        "surfaces": {"surface_1": {"x": 1}},
                    }
                },
            },
        )
        assert posted.status_code == 200, posted.text
        # a2uiClientDataModel is per-message (sent only to the server that
        # created the surface), not remembered on the session like
        # a2uiClientCapabilities -- stored on the posted USER message's own
        # metadata.
        user_id = posted.json()["message_id"]
        stored_message = next(m for m in client.app.state.messages.get(sid, []) if m.id == user_id)
        stored_data_models[sid] = stored_message.metadata[A2UI_CLIENT_DATA_MODEL_METADATA_KEY]

    browser_dm = stored_data_models[sid_browser]
    desktop_dm = stored_data_models[sid_desktop]
    assert (
        browser_dm
        == desktop_dm
        == {
            "version": "v0.9.1",
            "surfaces": {"surface_1": {"x": 1}},
        }
    )
