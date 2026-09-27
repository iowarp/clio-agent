"""A2 remote media: references resolve for a viewer on another machine.

Covers the server half of the A2 fix: the reference resolver
(``GET /v1/sessions/{sid}/references/resolve``) for every accepted URI form,
the percent-encoded ``custody_not_cas`` ``fetch_via`` redirect, the validator's
bare-id admission, and the producer export boundary that turns a workspace
file path into an artifact instead of a URL no remote viewer can fetch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_update_a2ui_components_tool,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.minting import mint_artifact
from clio_agent.gact.artifacts.records import (
    ArtifactKind,
    Custody,
    IdentityEvidence,
    Mechanism,
)

#: A file name carrying every character a naive query string mangles.
AWKWARD_NAME = "plot #1 & more+50%.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _workspace_session(c: TestClient, root: Path) -> tuple[str, str]:
    wid = c.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
    sid = c.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return wid, sid


def _pin(c: TestClient, sid: str, path: str) -> dict[str, Any]:
    response = c.post(f"/v1/sessions/{sid}/artifacts/pin", json={"path": path})
    assert response.status_code == 200, response.text
    return response.json()["pinned"]


def _upload_resource(c: TestClient, wid: str, name: str, data: bytes) -> str:
    created = c.post(
        f"/v1/workspaces/{wid}/resources",
        json={"name": name, "size": len(data), "media_type": "image/png"},
    )
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    patched = c.patch(
        f"/v1/workspaces/{wid}/resources/{rid}/content",
        content=data,
        headers={"Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"},
    )
    assert patched.status_code == 204, patched.text
    return rid


def _resolve(c: TestClient, sid: str, uri: str) -> Any:
    return c.get(f"/v1/sessions/{sid}/references/resolve", params={"uri": uri})


# --------------------------------------------------------------------------- #
# fetch_via: the custody redirect must survive a file name carrying &#+%
# --------------------------------------------------------------------------- #


def test_custody_redirect_fetch_via_is_percent_encoded_and_round_trips(tmp_path: Path) -> None:
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    target = tmp_path / "sub dir" / AWKWARD_NAME
    target.parent.mkdir()
    target.write_bytes(PNG)
    version = mint_artifact(
        c.app,
        sid,
        name=AWKWARD_NAME,
        workspace_id=wid,
        evidence=IdentityEvidence.hashed_at_use(
            sha256=hashlib.sha256(PNG).hexdigest(), size_bytes=len(PNG)
        ),
        kind=ArtifactKind.IMAGE,
        mechanism=Mechanism.HARNESS,
        custody=Custody.WORKSPACE_REFERENCED,
        path=str(target),
    )
    assert version is not None

    redirected = c.get(f"/v1/artifacts/{version.artifact_id}/bytes")
    assert redirected.status_code == 409
    fetch_via = redirected.json()["error"]["details"]["fetch_via"]
    # The whole relative path is ONE query value: no fragment cut, no extra keys.
    parts = urlsplit(fetch_via)
    assert parts.fragment == ""
    assert parse_qs(parts.query) == {"path": [f"sub dir/{AWKWARD_NAME}"]}
    # And following it returns the exact file, not a truncated / different path.
    followed = c.get(fetch_via)
    assert followed.status_code == 200, followed.text
    assert followed.content == PNG


# --------------------------------------------------------------------------- #
# The resolver: one test per accepted form
# --------------------------------------------------------------------------- #


@pytest.fixture
def pinned(tmp_path: Path) -> dict[str, Any]:
    c = _client(tmp_path)
    wid, sid = _workspace_session(c, tmp_path)
    (tmp_path / AWKWARD_NAME).write_bytes(PNG)
    version = _pin(c, sid, AWKWARD_NAME)
    rid = _upload_resource(c, wid, "site map #2 & legend.png", PNG)
    return {"c": c, "wid": wid, "sid": sid, "version": version, "rid": rid}


@pytest.mark.parametrize(
    "form",
    [
        "artifact://{aid}",
        "artifact:{aid}",
        "{aid}",
        "artifact://{wid}/{name}@v1",
        "artifact://{wid}/{name}@latest",
        "artifact://{wid}/{name}",
        "artifact://{wid}/{encoded}@v1",
    ],
)
def test_artifact_reference_forms_resolve_to_the_version(pinned: dict[str, Any], form: str) -> None:
    c, sid, version = pinned["c"], pinned["sid"], pinned["version"]
    aid = version["artifact_id"]
    uri = form.format(
        aid=aid,
        wid=pinned["wid"],
        name=AWKWARD_NAME,
        encoded="plot%20%231%20%26%20more%2B50%25.png",
    )

    response = _resolve(c, sid, uri)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "artifact"
    assert body["artifact_id"] == aid
    # workspace_id is SET, so a client's workspace fallback can engage.
    assert body["workspace_id"] == pinned["wid"]
    assert body["name"] == AWKWARD_NAME
    assert body["media_type"] == "image/png"
    assert body["size_bytes"] == len(PNG)
    assert body["fetch_path"] == f"/v1/artifacts/{aid}/bytes"
    assert c.get(body["fetch_path"]).content == PNG


@pytest.mark.parametrize(
    "form",
    ["resource://{wid}/{rid}", "resource://{rid}", "resource:{rid}", "{rid}"],
)
def test_resource_reference_forms_resolve_to_the_content_route(
    pinned: dict[str, Any], form: str
) -> None:
    c, sid, wid, rid = pinned["c"], pinned["sid"], pinned["wid"], pinned["rid"]

    response = _resolve(c, sid, form.format(wid=wid, rid=rid))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "resource"
    assert body["resource_id"] == rid
    assert body["workspace_id"] == wid
    assert body["name"] == "site map #2 & legend.png"
    assert body["media_type"] == "image/png"
    assert body["size_bytes"] == len(PNG)
    assert body["fetch_path"] == f"/v1/workspaces/{wid}/resources/{rid}/content"
    assert c.get(body["fetch_path"]).content == PNG


def test_unknown_references_are_typed_404s(pinned: dict[str, Any]) -> None:
    c, sid, wid = pinned["c"], pinned["sid"], pinned["wid"]
    for uri in (
        "artifact://artifact_0000",
        f"artifact://{wid}/missing.png@v1",
        "resource://res_0000",
    ):
        response = _resolve(c, sid, uri)
        assert response.status_code == 404, uri
        assert response.json()["error"]["error"] == "reference_not_found"


def test_unknown_version_names_the_resolvable_refs(pinned: dict[str, Any]) -> None:
    c, sid, wid = pinned["c"], pinned["sid"], pinned["wid"]
    response = _resolve(c, sid, f"artifact://{wid}/{AWKWARD_NAME}@v9")
    assert response.status_code == 404
    assert response.json()["error"]["details"]["available"] == ["latest", "v1"]


@pytest.mark.parametrize(
    "uri", ["https://example.org/x.png", "ui://ws/x@v1", "plot.png", "resource://ws/"]
)
def test_non_references_are_refused_typed(pinned: dict[str, Any], uri: str) -> None:
    response = _resolve(pinned["c"], pinned["sid"], uri)
    assert response.status_code == 422
    assert response.json()["error"]["error"] == "reference_uri_invalid"


def test_unknown_session_is_typed_404(pinned: dict[str, Any]) -> None:
    response = _resolve(pinned["c"], "sess_missing", "artifact://artifact_0000")
    assert response.status_code == 404
    assert response.json()["error"]["error"] == "not_found"


def test_parse_reference_keeps_an_at_sign_inside_the_name() -> None:
    from clio_agent.gact.artifacts.references import (  # noqa: PLC0415
        ReferenceResolutionError,
        parse_reference,
    )

    parsed = parse_reference("artifact://ws_1/run@2026.png@v3")
    assert (parsed.workspace_id, parsed.name, parsed.ref) == ("ws_1", "run@2026.png", "v3")
    with pytest.raises(ReferenceResolutionError):
        parse_reference("javascript:alert(1)")


# --------------------------------------------------------------------------- #
# Validator: bare CLIO ids are references, executable schemes stay refused
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("artifact_abc123", True),
        ("res_abc123", True),
        ("artifact:artifact_abc123", True),
        ("resource://ws/res_abc", True),
        ("https://example.org/a.png", True),
        ("javascript:alert(1)", False),
        ("http://example.org/a.png", False),
        ("data:text/html,x", False),
        ("plot.png", False),
        ("artifact_../etc", False),
    ],
)
def test_validator_url_allowlist(value: str, allowed: bool) -> None:
    from clio_agent.gact.a2ui_catalogs.validation import is_allowed_a2ui_url  # noqa: PLC0415

    assert is_allowed_a2ui_url(value) is allowed


# --------------------------------------------------------------------------- #
# Producer export boundary: a workspace path becomes an artifact reference
# --------------------------------------------------------------------------- #


def _producer_session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str, Path]:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    root = tmp_path / "ws"
    root.mkdir()
    c = _client(tmp_path)
    _wid, sid = _workspace_session(c, root)
    app = c.app
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: sid)
    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}}
    )
    remember_client_capabilities(app, sid, caps)
    return app, sid, root


def _image_surface(url: str) -> list[dict[str, Any]]:
    return [
        {"id": "root", "component": "Column", "children": ["img"]},
        {"id": "img", "component": "Image", "url": url},
    ]


@pytest.mark.parametrize("style", ["relative", "absolute", "file_url"])
def test_create_surface_exports_a_workspace_path_as_an_artifact(
    tmp_path: Path, monkeypatch: Any, style: str
) -> None:
    app, sid, root = _producer_session(tmp_path, monkeypatch)
    target = root / AWKWARD_NAME
    target.write_bytes(PNG)
    url = {
        "relative": AWKWARD_NAME,
        "absolute": str(target),
        "file_url": target.as_uri(),
    }[style]

    result = build_create_a2ui_surface_tool()(surface_id="plot", components=_image_surface(url))

    assert result.get("ok") is not False, result
    [exported] = result["exported_artifacts"]
    assert exported["component_id"] == "img"
    assert exported["property"] == "url"
    assert exported["path"] == url
    assert exported["name"] == AWKWARD_NAME
    assert exported["uri"] == f"artifact://{exported['artifact_id']}"
    # The persisted surface carries the reference, never the service-local path.
    surface = app.state.a2ui_store.get(sid, "plot")
    components = surface.messages[-1]["updateComponents"]["components"]
    assert components[1]["url"] == exported["uri"]
    # The export is a real registered artifact with the harness designation.
    record, version = app.state.artifact_registry.get_by_artifact_id(exported["artifact_id"])
    assert version.producer["designation"] == "a2ui-export"
    assert version.mechanism == Mechanism.HARNESS


def test_re_exporting_unchanged_bytes_reuses_the_artifact(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, root = _producer_session(tmp_path, monkeypatch)
    (root / "plot.png").write_bytes(PNG)
    create = build_create_a2ui_surface_tool()
    first = create(surface_id="plot", components=_image_surface("plot.png"))
    second = build_update_a2ui_components_tool()(
        surface_id="plot", components=_image_surface("plot.png")
    )
    assert (
        first["exported_artifacts"][0]["artifact_id"]
        == (second["exported_artifacts"][0]["artifact_id"])
    )


@pytest.mark.parametrize("url", ["missing.png", "../outside.png"])
def test_a_path_naming_no_workspace_file_is_a_grounded_refusal(
    tmp_path: Path, monkeypatch: Any, url: str
) -> None:
    app, sid, _root = _producer_session(tmp_path, monkeypatch)
    (tmp_path / "outside.png").write_bytes(PNG)

    result = build_create_a2ui_surface_tool()(surface_id="plot", components=_image_surface(url))

    assert result["ok"] is False
    assert result["reason"] == "a2ui_url_unresolved"
    assert "another machine" in result["detail"]
    assert result["hint"]
    # Nothing outside the workspace was read or registered.
    assert app.state.a2ui_store.get(sid, "plot") is None


def test_references_and_https_pass_through_unexported(tmp_path: Path, monkeypatch: Any) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)
    result = build_create_a2ui_surface_tool()(
        surface_id="plot", components=_image_surface("https://example.org/a.png")
    )
    assert result.get("ok") is not False, result
    assert "exported_artifacts" not in result


def test_an_executable_scheme_is_still_refused_by_the_validator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _app, _sid, _root = _producer_session(tmp_path, monkeypatch)
    result = build_create_a2ui_surface_tool()(
        surface_id="plot", components=_image_surface("javascript:alert(1)")
    )
    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert "javascript:alert(1)" in result["detail"]
