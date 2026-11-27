"""``plugin-asset://<plugin>/<path>``: a file a resolved plugin bundles, shown on a surface.

The guide plugin ships the docs' captures under ``assets/captures``; a surface names
one by this scheme and a viewer resolves it through the service, offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui_catalogs.validation import A2UIValidationError, _validate_url
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.references import ReferenceResolutionError
from clio_agent.gact.plugin_assets import parse

pytestmark = pytest.mark.usefixtures("native_builtin_lifespan")

CAPTURE = "captures/docs-answer-hook.webp"


def _session(tmp_path: Path) -> tuple[TestClient, str]:
    c = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(tmp_path)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return c, sid


def _resolve(c: TestClient, sid: str, uri: str) -> Any:
    return c.get(f"/v1/sessions/{sid}/references/resolve", params={"uri": uri})


def test_a_bundled_capture_resolves_and_its_bytes_are_served(tmp_path: Path) -> None:
    c, sid = _session(tmp_path)
    response = _resolve(c, sid, f"plugin-asset://guide/{CAPTURE}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "plugin_asset"
    assert body["plugin"] == "guide"
    assert body["media_type"] == "image/webp"
    assert body["name"] == "docs-answer-hook.webp"
    assert body["fetch_path"] == f"/v1/sessions/{sid}/plugin-assets/guide/{CAPTURE}"
    data = c.get(body["fetch_path"])
    assert data.status_code == 200
    assert data.headers["content-type"] == "image/webp"
    assert data.content[:4] == b"RIFF" and data.content[8:12] == b"WEBP"
    assert len(data.content) == body["size_bytes"]


def test_a_missing_asset_is_reference_not_found(tmp_path: Path) -> None:
    c, sid = _session(tmp_path)
    response = _resolve(c, sid, "plugin-asset://guide/captures/no-such.webp")
    assert response.status_code == 404
    assert "reference_not_found" in response.text


def test_a_plugin_outside_the_conversation_is_refused_typed(tmp_path: Path) -> None:
    c, sid = _session(tmp_path)
    response = _resolve(c, sid, "plugin-asset://not-a-plugin/x.png")
    assert response.status_code == 404
    assert "plugin_asset_plugin_unresolved" in response.text
    assert c.get(f"/v1/sessions/{sid}/plugin-assets/not-a-plugin/x.png").status_code == 404


def test_only_the_assets_folder_is_served(tmp_path: Path) -> None:
    c, sid = _session(tmp_path)
    # plugin.json sits at the plugin's root, outside assets/: never reachable.
    escaped = c.get(f"/v1/sessions/{sid}/plugin-assets/guide/..%2Fplugin.json")
    assert escaped.status_code in {404, 422}
    assert "plugin.json" not in escaped.text or "reference" in escaped.text


@pytest.mark.parametrize(
    "uri",
    [
        "plugin-asset://guide/../plugin.json",
        "plugin-asset:///captures/x.webp",
        "plugin-asset://guide/",
        "plugin-asset://guide/a\\b.webp",
        "plugin-asset://guide/x.webp?y=1",
    ],
)
def test_malformed_references_are_typed(uri: str) -> None:
    with pytest.raises(ReferenceResolutionError) as caught:
        parse(uri)
    assert caught.value.error == "reference_uri_invalid"


def test_the_surface_validator_admits_the_scheme() -> None:
    _validate_url(f"plugin-asset://guide/{CAPTURE}")
    with pytest.raises(A2UIValidationError, match="plugin-asset:"):
        _validate_url("ftp://example.org/x.png")
