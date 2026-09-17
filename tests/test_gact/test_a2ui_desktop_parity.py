"""S8 deliverable 6 (docs/design/a2ui-compat-campaign-2026-09.md, issue
#1374): desktop parity, server side.

The client-side architecture (owner decision 7, campaign doc:
``external/gact-tui/packages/core/src/v3/a2ui/client-metadata.ts``) already
states the invariant: "the transport never sees or builds this metadata, it
only carries whatever the repository layer merges in — identical by
construction," so browser (fetch) and Tauri (desktop webview IPC over the
same local HTTP API) send the EXACT SAME ``a2uiClientCapabilities``/
``a2uiClientDataModel`` JSON body. This module is the SERVER-side half of
that proof: ``src/clio_agent/gact/cors.py``'s origin allowlist
(``_DEFAULT_ORIGINS``) is the only place a request's transport/origin is
ever consulted, and it exists purely to gate BROWSER CORS preflight -- the
actual metadata parsing/remembering path
(``a2ui_capabilities.apply_client_metadata_guards``, called from
``message_submission.py``/the action dispatcher) never reads ``Origin`` or
any other transport-identifying header at all. Two fixture requests --
one with a real dev-server browser ``Origin`` header, one with a Tauri
desktop webview's headers (a ``tauri://localhost`` origin, no ``Origin``
header at all, and its own webview ``User-Agent`` -- Tauri requests
typically omit ``Origin`` for same-machine loopback IPC, which this proves
handles identically to a request that DOES carry one) -- must be remembered
byte-for-byte identically.
"""

from __future__ import annotations

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

#: A real browser dev-server request (the gact-tui web build, one of
#: ``cors.py``'s ``_DEFAULT_ORIGINS``).
BROWSER_HEADERS = {
    "Origin": "http://localhost:5173",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}
#: A real Tauri desktop webview request against the SAME local GACT server:
#: no ``Origin`` header at all (same-machine loopback IPC), its own webview
#: User-Agent string.
DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) clio-desktop/0.7.1 Tauri/2.0"
    ),
}


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
            "metadata": {
                "a2uiClientCapabilities": {
                    "v0.9": {"supportedCatalogIds": [BASIC_ID, WORKSPACE_ID]}
                }
            },
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
    assert (
        browser_stored
        == desktop_stored
        == {"v0.9": {"supportedCatalogIds": [BASIC_ID, WORKSPACE_ID]}}
    )

    browser_caps = client_capabilities(client.app, sid_browser)
    desktop_caps = client_capabilities(client.app, sid_desktop)
    assert browser_caps is not None
    assert desktop_caps is not None
    assert browser_caps.model_dump(mode="json") == desktop_caps.model_dump(mode="json")


def test_desktop_request_with_no_origin_header_is_not_treated_as_untrusted(
    client: TestClient,
) -> None:
    """Tauri's same-machine loopback IPC typically carries no ``Origin`` at
    all -- the metadata door must not silently degrade or refuse a request
    just because that header is absent (⚑ no-silent-fallback)."""

    sid = _create_session(client)
    headers = {k: v for k, v in DESKTOP_HEADERS.items() if k != "Origin"}
    assert "Origin" not in headers

    response = _post_capabilities(client, sid, headers)

    assert response.status_code == 200, response.text
    caps = client_capabilities(client.app, sid)
    assert caps is not None
    assert list(caps.v0_9.supportedCatalogIds) == [BASIC_ID, WORKSPACE_ID]


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
