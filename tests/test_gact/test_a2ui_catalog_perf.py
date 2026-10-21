"""A2UI catalog registry: discovery is cached, never repeated per message.

Adversarial review, BLOCKING perf: ``CatalogRegistry.get``/``.installed()``
used to re-discover every installed catalog (a real filesystem scan + parse)
on EVERY message, and the producibility resolver added a SECOND, independent
discovery per production door. Measured before the fix: POST updateComponents
#30 = 2.0s, transcript GET = 2.0s. The cache doctrine: caches accelerate, never
own truth; staleness is explicit (``CatalogRegistry.invalidate`` after a plugin
install), never implicit re-validation per call.

Discovery is now ``a2ui_catalogs.plugin.installed_plugin_catalogs`` over the
plugin store's records; loading one shipped catalog folder is
``a2ui_catalogs.plugin.load_catalog_directory``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui import project_a2ui_parts
from clio_agent.gact.a2ui_catalogs import plugin as plugin_catalogs
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part

from .a2ui_catalog_binding import MINIMAL_CATALOG_ID, MINIMAL_PLUGIN, bind_session_plugins

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def _create_message(surface_id: str = "surface_1", catalog_id: str = "") -> dict[str, object]:
    return {
        "version": "v0.9.1",
        "createSurface": {
            "surfaceId": surface_id,
            "catalogId": catalog_id or workspace_catalog_id(),
        },
    }


def _update_message(surface_id: str, index: int) -> dict[str, object]:
    return {
        "version": "v0.9.1",
        "updateDataModel": {"surfaceId": surface_id, "path": "/counter", "value": index},
    }


def _counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    """Wrap ``a2ui_catalogs.plugin.<name>`` to count real filesystem-discovery calls."""

    calls: list[int] = [0]
    real = getattr(plugin_catalogs, name)

    def _counted(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(plugin_catalogs, name, _counted)
    return calls


def test_discovery_is_called_at_most_once_across_50_message_production_folds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 sequential POSTs against an existing surface trigger at most one
    real installed-catalog discovery -- every subsequent producibility/registry
    lookup must hit the cache, not re-scan the filesystem."""

    calls = _counting(monkeypatch, "installed_plugin_catalogs")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="perf")
    client = TestClient(app)

    response = client.post(
        f"/v1/sessions/{session.id}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )
    assert response.status_code == 200, response.text

    for index in range(50):
        response = client.post(
            f"/v1/sessions/{session.id}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [_update_message("surface_1", index)]},
        )
        assert response.status_code == 200, response.text

    assert calls[0] <= 1, f"installed_plugin_catalogs called {calls[0]} times across 50 folds"


def test_discovery_is_called_at_most_once_across_50_transcript_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 session-transcript GETs (each projects A2UI surfaces) trigger at
    most one real installed-catalog discovery."""

    calls = _counting(monkeypatch, "installed_plugin_catalogs")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="perf-read")
    client = TestClient(app)
    client.post(
        f"/v1/sessions/{session.id}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message()]},
    )

    for _ in range(50):
        response = client.get(
            f"/v1/sessions/{session.id}/messages",
            headers={"X-GACT-Version": "0.3"},
        )
        assert response.status_code == 200, response.text

    assert calls[0] <= 1, f"installed_plugin_catalogs called {calls[0]} times across 50 reads"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "production gap: activation.resolve_session_catalogs re-runs "
        "declarations._load_declaration -> plugin.load_catalog_directory (3 file reads + JSON "
        "parse + checksum) for every directory catalog on every producibility lookup"
    ),
)
def test_plugin_catalog_surface_is_not_reloaded_from_disk_per_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surface on a catalog a session's plugin SHIPS: 50 updates load that
    catalog folder from disk at most once (the cached installed set serves it)."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="perf-plugin")
    bind_session_plugins(app, session.id, MINIMAL_PLUGIN)
    client = TestClient(app)
    response = client.post(
        f"/v1/sessions/{session.id}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(catalog_id=MINIMAL_CATALOG_ID)]},
    )
    assert response.status_code == 200, response.text

    calls = _counting(monkeypatch, "load_catalog_directory")
    for index in range(50):
        response = client.post(
            f"/v1/sessions/{session.id}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [_update_message("surface_1", index)]},
        )
        assert response.status_code == 200, response.text

    assert calls[0] <= 1, f"load_catalog_directory called {calls[0]} times across 50 folds"


def test_registry_get_resolves_builtins_with_zero_discovery_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A builtin catalogId never triggers installed-catalog discovery at all."""

    calls = _counting(monkeypatch, "installed_plugin_catalogs")
    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

    registry = CatalogRegistry()
    assert registry.get(workspace_catalog_id()) is not None
    assert calls[0] == 0


def test_fold_resolves_each_distinct_catalog_id_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """``project_a2ui_parts`` memoizes catalog resolution per call: N messages
    against the SAME catalog id must not re-invoke the resolver N times."""

    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

    registry = CatalogRegistry()
    lookups: list[tuple[str, str]] = []
    real_get = registry.get

    def _counted_get(catalog_id: str, protocol_version: str = "0.9.1"):  # type: ignore[no-untyped-def]
        lookups.append((catalog_id, protocol_version))
        return real_get(catalog_id, protocol_version)

    monkeypatch.setattr(registry, "get", _counted_get)

    parts = [
        Part(
            id=f"part_{i}",
            type="a2ui",
            surface_id="surface_1",
            a2ui_protocol_version="0.9.1",
            a2ui_messages=[_create_message() if i == 0 else _update_message("surface_1", i)],
        )
        for i in range(20)
    ]

    project_a2ui_parts(parts, "sess_memo", catalogs=registry)

    # createSurface resolves once explicitly; every later updateDataModel on
    # the SAME surface must reuse that resolution rather than re-querying.
    assert len(lookups) <= 2, f"catalog resolver invoked {len(lookups)} times for one catalog id"
