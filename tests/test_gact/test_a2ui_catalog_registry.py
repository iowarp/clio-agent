"""A2UI catalog registry, per-catalog validation, and blueprint declaration (S2).

docs/design/a2ui-compat-campaign-2026-09.md S2 / iowarp/clio-agent#1365: the
server trusts a catalog by registry lookup, not string equality against one
hard-coded id. See ``src/clio_agent/gact/a2ui_catalogs/`` for the registry,
blueprint declaration/loading, session producibility, and validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.a2ui import (
    A2UICatalogNotProducibleError,
    A2UICatalogUnknownError,
    project_a2ui_parts,
    validate_server_message,
)
from clio_agent.gact.a2ui_catalogs.activation import session_producible_catalog_ids
from clio_agent.gact.a2ui_catalogs.blueprint import (
    load_blueprint_catalogs,
    validate_blueprint_catalogs,
)
from clio_agent.gact.a2ui_catalogs.builtin import (
    basic_catalog_id,
    workspace_catalog_id,
)
from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry
from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root
from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.types import Message

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}

FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "a2ui_packs" / "minimal"


def _session_client(tmp_path: Path) -> tuple[TestClient, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="A2UI catalogs")
    return TestClient(app), session.id


def _create_message(catalog_id: str, surface_id: str = "surface_1") -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": catalog_id},
    }


def _install_isolated_minimal_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, Any]]:
    """Install the ``a2ui-minimal-pack`` fixture into a tmp-isolated user config dir.

    Adversarial (S7 composability) review finding #13: ``install_agent_
    blueprint``'s install root resolves through ``paths.user_config_dir_for
    (home, os.environ)``, which checks ``CLIO_USER_DIR``/``XDG_CONFIG_HOME``
    in ``os.environ`` BEFORE ever consulting the injected ``home`` argument --
    a bare ``Path.home`` monkeypatch does NOT redirect it on Windows
    (``%LOCALAPPDATA%\\clio-agent`` wins over an unset override), so a test
    that only patches ``Path.home`` can install fixture packs into the
    developer's REAL Agent Blueprint registry. ``tests/conftest.py``'s
    autouse ``allow_pytest_tmp_path`` fixture already sets both env vars
    process-wide, but this helper sets them explicitly too (belt-and-
    suspenders self-contained isolation, not an implicit dependency on
    fixture ordering elsewhere) and asserts, after the install, that the
    pack never landed under the REAL (unpatched) per-user registry.
    """

    import os

    from clio_agent import paths
    from clio_agent.gact.agent_blueprints import install_agent_blueprint

    # Captured BEFORE any patch below: the REAL, unpatched per-user registry
    # root, so the post-install assertion checks the actual machine rather
    # than a path relative to this test's own overrides.
    real_root = paths.user_config_dir_for(Path.home(), dict(os.environ))

    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    result = install_agent_blueprint(
        source=str(FIXTURE_PACK),
        scope="global",
        cwd=cwd,
        home=home,
        blueprint_id="a2ui-minimal-pack",
    )

    leaked = real_root / "agent-blueprints" / "a2ui-minimal-pack"
    assert not leaked.exists(), f"fixture pack leaked into the real registry: {leaked}"
    return home, cwd, result


# --------------------------------------------------------------------------- #
# Builtin registry
# --------------------------------------------------------------------------- #


def test_builtin_registry_has_exactly_the_two_catalogs() -> None:
    registry = CatalogRegistry()
    ids = {entry.catalog_id for entry in registry.builtin()}
    assert ids == {basic_catalog_id(), workspace_catalog_id()}
    for entry in registry.builtin():
        assert entry.source == "builtin"
        assert entry.protocol_version == "0.9.1"
        assert entry.validators  # at least one compiled component validator


def test_registry_get_resolves_both_builtins_and_unknown_is_none() -> None:
    registry = CatalogRegistry()
    assert registry.get(basic_catalog_id()) is not None
    assert registry.get(workspace_catalog_id()) is not None
    assert registry.get("https://example.test/not-installed") is None
    assert registry.get(workspace_catalog_id(), protocol_version="1.0") is None


# --------------------------------------------------------------------------- #
# Unknown catalogId -> 422 a2ui_catalog_unknown
# --------------------------------------------------------------------------- #


def test_unknown_catalog_id_is_rejected_with_typed_reason(tmp_path: Path) -> None:
    client, sid = _session_client(tmp_path)

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message("https://example.test/nope")]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_catalog_unknown"


def test_validate_server_message_raises_typed_error_for_unknown_catalog() -> None:
    registry = CatalogRegistry()
    with pytest.raises(A2UICatalogUnknownError) as excinfo:
        validate_server_message(_create_message("https://example.test/nope"), catalogs=registry)
    assert excinfo.value.catalog_id == "https://example.test/nope"


# --------------------------------------------------------------------------- #
# Blueprint-declared pack catalog: producible only when activated
# --------------------------------------------------------------------------- #


def test_pack_catalog_producible_only_in_a_session_that_activated_its_blueprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack catalog installed server-wide is KNOWN to every session (``get``
    resolves it, so replay/validation never breaks) but only PRODUCIBLE —
    creatable via ``createSurface`` — in a session whose own active blueprint
    declared it."""

    _install_isolated_minimal_pack(tmp_path, monkeypatch)

    client, sid = _session_client(tmp_path)
    app = client.app
    installed = app.state.a2ui_catalogs.get("https://example.test/a2ui/catalogs/minimal")
    assert installed is not None
    pack_catalog_id = installed.catalog_id

    # Installed server-wide, but this session never activated the pack.
    assert pack_catalog_id not in session_producible_catalog_ids(app, sid)

    unactivated = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(pack_catalog_id)]},
    )
    assert unactivated.status_code == 422
    assert unactivated.json()["error"]["error"] == "a2ui_catalog_not_producible"

    app.state.sessions.update(
        sid, metadata_patch={"active_agent_blueprint_id": "a2ui-minimal-pack"}
    )
    assert pack_catalog_id in session_producible_catalog_ids(app, sid)

    activated = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(pack_catalog_id)]},
    )
    assert activated.status_code == 200, activated.text


def test_path_activated_pack_catalog_is_producible_before_install() -> None:
    """A session-scoped PATH activation (a pack referenced by an on-disk path,
    not yet copied into the installed registry — the mechanism marketplace
    packs under test use, mirroring ``resolve_active_blueprint_servers`` for
    MCP servers) makes its own catalog resolvable AND producible for that
    session even though the app-level registry never discovered it."""

    from unittest.mock import MagicMock

    from clio_agent.gact.a2ui_catalogs.activation import session_catalog_resolver

    blueprint = parse_agent_blueprint_root(FIXTURE_PACK, scope="session")
    pack_catalog_id = load_blueprint_catalogs(blueprint)[0].catalog_id

    app = MagicMock()
    app.state.a2ui_catalogs = CatalogRegistry()
    session = MagicMock()
    session.metadata = {
        "active_agent_blueprint_id": blueprint.id,
        "active_agent_blueprint_path": str(FIXTURE_PACK),
    }
    app.state.sessions.get.return_value = session

    assert app.state.a2ui_catalogs.get(pack_catalog_id) is None  # not installed server-wide
    assert pack_catalog_id in session_producible_catalog_ids(app, "sess_path")
    assert session_catalog_resolver(app, "sess_path").get(pack_catalog_id) is not None


def test_apply_batch_raises_typed_error_when_catalog_is_installed_but_not_producible() -> None:
    blueprint = parse_agent_blueprint_root(FIXTURE_PACK, scope="session")
    entry = load_blueprint_catalogs(blueprint)[0]

    class _StubResolver:
        def get(self, catalog_id: str, protocol_version: str = "0.9.1") -> Any:
            return entry if catalog_id == entry.catalog_id else None

    with pytest.raises(A2UICatalogNotProducibleError) as excinfo:
        validate_server_message(
            _create_message(entry.catalog_id),
            catalogs=_StubResolver(),
            producible=frozenset({workspace_catalog_id(), basic_catalog_id()}),
        )
    assert excinfo.value.catalog_id == entry.catalog_id


def test_pack_catalog_aliases_basic_components(tmp_path: Path) -> None:
    """The minimal pack's ``MinimalText``/``MinimalButton`` validate like their
    aliased Basic kernels: same required shape, closed against extras."""

    blueprint = parse_agent_blueprint_root(FIXTURE_PACK, scope="session")
    entry = load_blueprint_catalogs(blueprint)[0]
    assert set(entry.sidecar.implements) == {"MinimalText", "MinimalButton"}
    assert entry.sidecar.implements["MinimalText"].kernel == "Text"
    assert entry.sidecar.implements["MinimalButton"].kernel == "Button"

    assert entry.validators["MinimalText"].is_valid(
        {"id": "t", "component": "MinimalText", "text": "hi"}
    )
    assert not entry.validators["MinimalText"].is_valid({"id": "t", "component": "MinimalText"})


# --------------------------------------------------------------------------- #
# A pack whose implements[].kernel names a nonexistent component
# --------------------------------------------------------------------------- #


def _write_broken_pack(tmp_path: Path) -> Path:
    root = tmp_path / "broken-pack"
    (root / "catalogs" / "broken").mkdir(parents=True)
    (root / "experts").mkdir()
    (root / "AGENT.md").write_text(
        "---\n"
        "id: a2ui-broken-pack\n"
        "title: Broken Pack\n"
        "root_expert: root\n"
        "a2ui_catalogs:\n"
        "  broken: catalogs/broken\n"
        "blueprint:\n"
        "  format: agent-blueprint-v1\n"
        "---\n\nBroken test pack.\n",
        encoding="utf-8",
    )
    (root / "experts" / "root.md").write_text(
        "---\nid: root\ntitle: Root\ntier: 1\nmodule:\n  kind: react\ntools: []\n---\n\nRoot.\n",
        encoding="utf-8",
    )
    (root / "catalogs" / "broken" / "catalog.json").write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
        '"$id": "https://example.test/a2ui/catalogs/broken", '
        '"title": "Broken", "description": "Broken.", '
        '"catalogId": "https://example.test/a2ui/catalogs/broken", '
        '"components": {"Ghost": {"type": "object", "properties": '
        '{"component": {"const": "Ghost"}}, "required": ["component"]}}, '
        '"functions": {}, "$defs": {}}',
        encoding="utf-8",
    )
    (root / "catalogs" / "broken" / "catalog.clio.json").write_text(
        '{"catalogId": "https://example.test/a2ui/catalogs/broken", '
        '"protocolVersion": "0.9.1", "trust": {"source": "pack"}, '
        '"implements": {"Ghost": {"kernel": "NoSuchComponent"}}, '
        '"events": {}, "instructions": "instructions.md"}',
        encoding="utf-8",
    )
    (root / "catalogs" / "broken" / "instructions.md").write_text("Broken.\n", encoding="utf-8")
    return root


def test_pack_catalog_with_unimplemented_kernel_is_refused_at_validate_time(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path

    root = _write_broken_pack(tmp_path)

    result = validate_agent_blueprint_path(root, scope="session")

    assert result["enabled"] is False
    assert result["validation_errors"]
    assert any("NoSuchComponent" in e for e in result["validation_errors"])

    blueprint = parse_agent_blueprint_root(root, scope="session")
    errors = validate_blueprint_catalogs(blueprint)
    assert any("NoSuchComponent" in e for e in errors)
    assert load_blueprint_catalogs(blueprint) == []  # a broken pack catalog never loads


# --------------------------------------------------------------------------- #
# Representative official Basic catalog messages validate against the Basic entry
# --------------------------------------------------------------------------- #

_BASIC_EXAMPLE_MESSAGES: list[dict[str, Any]] = [
    {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "s",
            "components": [
                {"id": "root", "component": "Column", "children": ["greeting"]},
                {"id": "greeting", "component": "Text", "text": "Hello, world!"},
            ],
        },
    },
    {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "s",
            "components": [
                {
                    "id": "submit",
                    "component": "Button",
                    "child": "label",
                    "action": {"event": {"name": "form.submit", "context": {}}},
                },
                {"id": "label", "component": "Text", "text": "Submit"},
            ],
        },
    },
    {
        "version": "v0.9.1",
        "updateDataModel": {"surfaceId": "s", "path": "/greeting", "value": "Updated"},
    },
]


@pytest.mark.parametrize("message", _BASIC_EXAMPLE_MESSAGES)
def test_representative_basic_catalog_messages_validate(message: dict[str, Any]) -> None:
    """Representative official Basic-catalog messages validate through the
    server's own ``validate_server_message`` with the Basic entry.

    This deliberately does not re-vendor clio-schemas' full 43-example / ajv
    corpus (that conformance proof lives in clio-schemas itself, S1 —
    docs/design/a2ui-compat-campaign-2026-09.md's Codex-doc ledger row #17);
    it proves the SAME machinery (``entry.validators`` compiled by
    ``clio_schemas.a2ui.validation.catalog_validators``) this server actually
    calls accepts shapes drawn from that catalog.
    """

    registry = CatalogRegistry()
    basic_entry = registry.get(basic_catalog_id())
    operation, surface_id = validate_server_message(
        message, catalogs=registry, catalog_entry=basic_entry
    )
    assert surface_id == "s"
    assert operation in {"updateComponents", "updateDataModel"}


# --------------------------------------------------------------------------- #
# functionCall: allowed iff declared by the catalog
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("a2ui_builtin_catalogs")
def test_declared_function_call_passes_and_undeclared_fails(tmp_path: Path) -> None:
    client, sid = _session_client(tmp_path)
    registry = client.app.state.a2ui_catalogs
    assert "required" in registry.get(workspace_catalog_id()).file["functions"]
    assert "nope" not in registry.get(workspace_catalog_id()).file["functions"]

    ok = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "field",
                    "component": "TextField",
                    "label": "Name",
                    "checks": [
                        {
                            "condition": {
                                "call": "required",
                                "args": {"value": {"path": "/name"}},
                                "returnType": "boolean",
                            },
                            "message": "Required",
                        }
                    ],
                }
            ],
        },
    }
    bad = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {"id": "text", "component": "Text", "text": {"call": "nope", "args": {}}}
            ],
        },
    }

    ok_response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(workspace_catalog_id()), ok]},
    )
    assert ok_response.status_code == 200, ok_response.text

    bad_response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [bad]},
    )
    assert bad_response.status_code == 422
    assert "nope" in bad_response.json()["error"]["message"]


def test_undeclared_function_call_maps_to_typed_wire_error_code(tmp_path: Path) -> None:
    """A functionCall in a schema-UNREACHABLE zone (updateDataModel.value) is
    caught by the safety walk's A2UIFunctionNotInCatalogError, which the HTTP
    door maps to the a2ui_function_not_in_catalog wire error code (adversarial
    S2 review) -- not the generic a2ui_validation_failed."""

    client, sid = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(workspace_catalog_id())]},
    )
    smuggled_call = {
        "version": "v0.9.1",
        "updateDataModel": {
            "surfaceId": "surface_1",
            "path": "/x",
            "value": {"call": "nope", "args": {}},
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [smuggled_call]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_function_not_in_catalog"
    reasons = client.app.state.a2ui_catalogs.session_reasons(sid)
    assert any(row["reason"] == "a2ui_function_not_in_catalog" for row in reasons)


# --------------------------------------------------------------------------- #
# Named pointer on a map latitude violation
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("a2ui_builtin_catalogs")
def test_map_latitude_out_of_range_names_pointer(tmp_path: Path) -> None:
    client, sid = _session_client(tmp_path)
    invalid = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "map",
                    "component": "clio.map.v1",
                    "points": [{"id": "s1", "label": "Bad", "latitude": 200, "longitude": -87.63}],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(workspace_catalog_id()), invalid]},
    )

    assert response.status_code == 422
    assert "pointer=/points/0/latitude" in response.json()["error"]["message"]


# --------------------------------------------------------------------------- #
# Replay: a v0.9.1 surface fixture folds unchanged; an uninstalled catalog
# folds as state=unknown with a2ui_catalog_unavailable, never quarantined
# --------------------------------------------------------------------------- #


def _a2ui_part(*, part_id: str, surface_id: str, catalog_id: str) -> Part:
    return Part(
        id=part_id,
        type="a2ui",
        surface_id=surface_id,
        a2ui_protocol_version="0.9.1",
        a2ui_messages=[_create_message(catalog_id, surface_id)],
    )


def test_persisted_surface_on_an_installed_catalog_replays_unchanged() -> None:
    registry = CatalogRegistry()
    part = _a2ui_part(part_id="part_ok", surface_id="surface_ok", catalog_id=workspace_catalog_id())

    surfaces, degradations = project_a2ui_parts([part], "sess_replay", catalogs=registry)

    assert degradations == []
    record = surfaces[("sess_replay", "surface_ok")]
    assert record.state == "creating"
    assert record.catalog_id == workspace_catalog_id()


def test_persisted_surface_on_an_uninstalled_catalog_folds_as_unknown_never_dropped() -> None:
    registry = CatalogRegistry()
    part = _a2ui_part(
        part_id="part_gone",
        surface_id="surface_gone",
        catalog_id="https://example.test/uninstalled",
    )

    surfaces, degradations = project_a2ui_parts([part], "sess_replay", catalogs=registry)

    assert len(degradations) == 1
    assert degradations[0]["code"] == "a2ui_catalog_unavailable"
    record = surfaces[("sess_replay", "surface_gone")]
    assert record.state == "unknown"
    assert record.catalog_id == "https://example.test/uninstalled"
    assert record.messages  # the raw messages are preserved, not dropped


# --------------------------------------------------------------------------- #
# Catalog discovery routes
# --------------------------------------------------------------------------- #


def test_installed_catalogs_route_lists_both_builtins(tmp_path: Path) -> None:
    client, _sid = _session_client(tmp_path)

    response = client.get("/v1/a2ui/catalogs")

    assert response.status_code == 200
    ids = {row["catalogId"] for row in response.json()["catalogs"]}
    assert ids == {basic_catalog_id(), workspace_catalog_id()}
    for row in response.json()["catalogs"]:
        assert row["componentNames"]
        assert "sidecar" in row
        assert "producible" not in row  # session-less: no producibility verdict


@pytest.mark.usefixtures("a2ui_builtin_catalogs")
def test_session_catalogs_route_reports_producibility_and_full_shape(tmp_path: Path) -> None:
    client, sid = _session_client(tmp_path)

    response = client.get(f"/v1/sessions/{sid}/a2ui/catalogs")

    assert response.status_code == 200
    rows = {row["catalogId"]: row for row in response.json()["catalogs"]}
    workspace_row = rows[workspace_catalog_id()]
    assert workspace_row["producible"] is True
    assert "file" in workspace_row
    assert "instructions" in workspace_row


def test_session_catalogs_route_404s_on_unknown_session(tmp_path: Path) -> None:
    client, _sid = _session_client(tmp_path)

    response = client.get("/v1/sessions/sess_does_not_exist/a2ui/catalogs")

    assert response.status_code == 404


def _assert_no_json_null(value: Any, path: str = "$") -> None:
    """Recursively fail if a JSON ``null`` appears anywhere under ``value``."""

    if value is None:
        pytest.fail(f"unexpected JSON null at {path} -- wire contract is absent, never null")
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_json_null(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_json_null(child, f"{path}[{index}]")


def test_installed_catalogs_route_sidecar_has_no_null_anywhere(tmp_path: Path) -> None:
    """S1 A2UI catalog contract: "absent, never null" (docs/design bug A).

    Before ``exclude_none=True`` on ``entry.sidecar.model_dump(...)``
    (``a2ui_catalogs/routes/a2ui_catalogs.py``), every builtin catalog's
    ``implements`` map serialises its unset ``presets`` field as JSON
    ``null`` (once per unaliased component -- 18 times for Basic, 30 for the
    CLIO workspace catalog), and every declared ``events`` route serialises
    its unset ``context_schema``/``operation``/``narration`` fields the same
    way. A client zod schema built with ``.optional()`` (which REJECTS an
    explicit ``null``) then fails to parse the WHOLE catalog list and
    silently empties its registry (gact-tui's ``catalog-registry.ts``) -- the
    exact root cause of "Interactive surface unavailable" on the default
    agent even though the producer tool reports ``created: true``. Entered
    via ``with TestClient(app) as c`` against the real route, not a
    hand-built dict, so this exercises the actual served bytes.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        response = client.get("/v1/a2ui/catalogs")

    assert response.status_code == 200
    rows = response.json()["catalogs"]
    assert {row["catalogId"] for row in rows} == {basic_catalog_id(), workspace_catalog_id()}
    for row in rows:
        # Both builtins declare a non-empty `implements` map with at least
        # one entry that omits `presets` -- if this ever stopped being true
        # the null-serialisation bug would go unexercised without failing.
        assert row["sidecar"]["implements"], row["catalogId"]
        _assert_no_json_null(row["sidecar"], path=f"$.catalogs[{row['catalogId']}].sidecar")


def test_session_catalogs_route_sidecar_has_no_null_anywhere(tmp_path: Path) -> None:
    """Same "absent, never null" contract on the session-scoped route."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="A2UI null-sidecar")
        response = client.get(f"/v1/sessions/{session.id}/a2ui/catalogs")

    assert response.status_code == 200
    rows = response.json()["catalogs"]
    assert rows
    for row in rows:
        _assert_no_json_null(row["sidecar"], path=f"$.catalogs[{row['catalogId']}].sidecar")


# --------------------------------------------------------------------------- #
# Adversarial review follow-ups: reason retrievability, declared destination,
# per-expert subset validation, UAX#31 warning, catalog-fixed-per-surface,
# session isolation, install-checksum stamping
# --------------------------------------------------------------------------- #


def test_replay_catalog_unavailable_is_recorded_in_the_retrievable_session_ledger() -> None:
    """The replay fold's a2ui_catalog_unavailable degradation routes through
    the SAME per-session ledger the HTTP door uses (adversarial S2 review) --
    not just the returned degradations list."""

    app = build_app(sessions_path=None)
    session = app.state.sessions.create(workspace_id="ws_default", title="unavailable")
    part = _a2ui_part(
        part_id="part_gone",
        surface_id="surface_gone",
        catalog_id="https://example.test/uninstalled",
    )
    app.state.messages[session.id] = [
        Message(
            id="msg_gone",
            session_id=session.id,
            role="assistant",
            created_at="2026-09-17T00:00:00Z",
            updated_at="2026-09-17T00:00:00Z",
            parts=[part],
        )
    ]

    surfaces, degradations = app.state.a2ui_store.list_wire_with_degradations(session.id)

    assert any(row["code"] == "a2ui_catalog_unavailable" for row in degradations)
    reasons = app.state.a2ui_catalogs.session_reasons(session.id)
    assert any(row["reason"] == "a2ui_catalog_unavailable" for row in reasons)


@pytest.mark.usefixtures("a2ui_builtin_catalogs")
def test_declared_agent_destination_is_not_recorded_as_undeclared(tmp_path: Path) -> None:
    """An event name the sidecar routes to a non-default destination is
    DECLARED; only a name the sidecar never mentions gets the
    a2ui_event_destination_undeclared reason (adversarial S2 review fixed a
    bug that recorded it for every plain agent-destined event)."""

    client, sid = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(workspace_catalog_id())]},
    )
    declared_action = {
        "version": "v0.9.1",
        "action": {
            "name": "run.cancel",
            "surfaceId": "surface_1",
            "sourceComponentId": "c",
            "timestamp": "2026-09-17T00:00:00Z",
            "context": {},
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": declared_action}
    )

    assert response.status_code == 200, response.text
    reasons = client.app.state.a2ui_catalogs.session_reasons(sid)
    assert not any(row["reason"] == "a2ui_event_destination_undeclared" for row in reasons)


def _write_pack(tmp_path: Path, *, pack_id: str, component_name: str) -> Path:
    """A minimal installable-shape pack aliasing Basic ``Text`` under
    ``component_name``, with two experts: ``root`` (declares the catalog)
    and ``consumer`` (whose ``a2ui_catalogs`` subset is set by the caller)."""

    root = tmp_path / pack_id
    (root / "catalogs" / "solo").mkdir(parents=True)
    (root / "experts").mkdir()
    (root / "AGENT.md").write_text(
        f"---\nid: {pack_id}\ntitle: {pack_id}\nroot_expert: root\n"
        "a2ui_catalogs:\n  solo: catalogs/solo\nblueprint:\n  format: agent-blueprint-v1\n"
        f"---\n\n{pack_id} test pack.\n",
        encoding="utf-8",
    )
    (root / "experts" / "root.md").write_text(
        "---\nid: root\ntitle: Root\ntier: 1\nmodule:\n  kind: react\ntools: []\n---\n\nRoot.\n",
        encoding="utf-8",
    )
    (root / "catalogs" / "solo" / "catalog.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"https://example.test/a2ui/catalogs/{pack_id}",
                "title": pack_id,
                "description": "Test.",
                "catalogId": f"https://example.test/a2ui/catalogs/{pack_id}",
                "components": {
                    component_name: {
                        "type": "object",
                        "properties": {
                            "component": {"const": component_name},
                            "text": {"type": "string"},
                        },
                        "required": ["component"],
                    }
                },
                "functions": {},
                "$defs": {},
            }
        ),
        encoding="utf-8",
    )
    (root / "catalogs" / "solo" / "catalog.clio.json").write_text(
        json.dumps(
            {
                "catalogId": f"https://example.test/a2ui/catalogs/{pack_id}",
                "protocolVersion": "0.9.1",
                "trust": {"source": "pack"},
                "implements": {component_name: {"kernel": "Text"}},
                "events": {},
                "instructions": "instructions.md",
            }
        ),
        encoding="utf-8",
    )
    (root / "catalogs" / "solo" / "instructions.md").write_text("Test.\n", encoding="utf-8")
    return root


def test_expert_a2ui_catalogs_subset_with_undeclared_name_is_a_validation_error(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path

    root = _write_pack(tmp_path, pack_id="a2ui-expert-subset-pack", component_name="SoloText")
    (root / "experts" / "consumer.md").write_text(
        "---\nid: consumer\ntitle: Consumer\ntier: 2\nparent_id: root\n"
        "module:\n  kind: react\ntools: []\na2ui_catalogs: [nonexistent]\n---\n\nConsumer.\n",
        encoding="utf-8",
    )

    result = validate_agent_blueprint_path(root, scope="session")

    assert result["enabled"] is False
    assert any(
        "consumer" in e and "undeclared a2ui catalog" in e for e in result["validation_errors"]
    )


def test_expert_a2ui_catalogs_subset_with_declared_name_is_valid(tmp_path: Path) -> None:
    from clio_agent.gact.agent_blueprints import validate_agent_blueprint_path

    root = _write_pack(tmp_path, pack_id="a2ui-expert-subset-ok-pack", component_name="SoloText")
    (root / "experts" / "consumer.md").write_text(
        "---\nid: consumer\ntitle: Consumer\ntier: 2\nparent_id: root\n"
        "module:\n  kind: react\ntools: []\na2ui_catalogs: [solo]\n---\n\nConsumer.\n",
        encoding="utf-8",
    )

    result = validate_agent_blueprint_path(root, scope="session")

    assert result["enabled"] is True, result["validation_errors"]


def test_dotted_pack_component_name_records_uax31_warning_not_an_error(tmp_path: Path) -> None:
    """0.9.1 tolerates a non-UAX#31 component name; it is a recorded WARNING
    reason, never a blocking validation error."""

    root = _write_pack(tmp_path, pack_id="a2ui-dotted-pack", component_name="my.dotted.v1")

    blueprint = parse_agent_blueprint_root(root, scope="session")
    errors = validate_blueprint_catalogs(blueprint)
    assert errors == []
    entries = load_blueprint_catalogs(blueprint)
    assert len(entries) == 1

    from clio_agent.gact.a2ui_catalogs.reasons import recorded_a2ui_catalog_reasons

    reasons = recorded_a2ui_catalog_reasons()
    matches = [
        row
        for row in reasons
        if row["reason"] == "a2ui_identifier_not_uax31" and row.get("component") == "my.dotted.v1"
    ]
    assert matches
    assert matches[-1]["severity"] == "info"


@pytest.mark.usefixtures("a2ui_builtin_catalogs")
def test_basic_surface_rejects_a_workspace_only_component(tmp_path: Path) -> None:
    """A surface's catalog is fixed for its lifetime: a Basic-catalog surface
    cannot later accept a clio.* (workspace-only) component."""

    client, sid = _session_client(tmp_path)
    create_on_basic = {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": "surface_1", "catalogId": basic_catalog_id()},
    }
    workspace_only_update = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "surface_1",
            "components": [
                {
                    "id": "map",
                    "component": "clio.map.v1",
                    "points": [{"id": "s1", "label": "x", "latitude": 1, "longitude": 1}],
                }
            ],
        },
    }

    response = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [create_on_basic, workspace_only_update]},
    )

    assert response.status_code == 422
    assert "clio.map.v1" in response.json()["error"]["message"]
    assert basic_catalog_id() in response.json()["error"]["message"]


def test_second_session_with_no_active_blueprint_cannot_produce_the_pack_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producibility is SESSION-scoped: activating a pack in one session must
    not leak into a second, unrelated session on the same app."""

    _install_isolated_minimal_pack(tmp_path, monkeypatch)

    client, activated_sid = _session_client(tmp_path)
    app = client.app
    app.state.sessions.update(
        activated_sid, metadata_patch={"active_agent_blueprint_id": "a2ui-minimal-pack"}
    )
    other_session = app.state.sessions.create(workspace_id="ws_default", title="unrelated")

    pack_catalog_id = "https://example.test/a2ui/catalogs/minimal"
    assert pack_catalog_id in session_producible_catalog_ids(app, activated_sid)
    assert pack_catalog_id not in session_producible_catalog_ids(app, other_session.id)

    response = client.post(
        f"/v1/sessions/{other_session.id}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [_create_message(pack_catalog_id)]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["error"] == "a2ui_catalog_not_producible"


def test_pack_catalog_entry_carries_the_blueprint_install_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stamped like blueprint_server_map's CLIO_BLUEPRINT_INSTALL_CHECKSUM:
    the entry's install_checksum names the OWNING pack version, distinct from
    its own catalog-file content checksum."""

    _, _, installed = _install_isolated_minimal_pack(tmp_path, monkeypatch)
    expected_checksum = installed["installed"][0]["install"]["checksum"]
    assert expected_checksum

    app = build_app(sessions_path=tmp_path / "sessions.json")
    entry = app.state.a2ui_catalogs.get("https://example.test/a2ui/catalogs/minimal")

    assert entry is not None
    assert entry.install_checksum == expected_checksum
    assert entry.install_checksum != entry.checksum
