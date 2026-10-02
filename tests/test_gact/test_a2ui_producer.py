"""A2UI producer tools (S4, docs/design/a2ui-compat-campaign-2026-09.md).

Round-trips the four producer tools (create/update-components/update-data-
model/delete) through the transcript-owned surface store, and proves every
producer mistake comes back as a typed refusal dict, never an exception.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_delete_a2ui_surface_tool,
    build_inspect_a2ui_surface_tool,
    build_update_a2ui_components_tool,
    build_update_a2ui_data_model_tool,
)
from clio_agent.gact.app import build_app

WORKSPACE_ID = workspace_catalog_id()
BASIC_ID = basic_catalog_id()


def _session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    # The store seeds "ws_default" with root_path=os.getcwd() (workspaces.py
    # _seed_default) so a bare TUI boot always has something to render — but a
    # producer-tool test must never write through that into the real
    # repo/invocation cwd (e.g. a minted surface-definition artifact, #1533
    # S4). Rebind it to this test's own tmp_path before any tool call runs.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    session = app.state.sessions.create(workspace_id="ws_default", title="A2UI producer")
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session.id)
    return app, session.id


def _advertise_workspace_catalog(app: Any, session_id: str) -> None:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [WORKSPACE_ID]}})
    remember_client_capabilities(app, session_id, caps)


def _advertise_basic_catalog_only(app: Any, session_id: str) -> None:
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": [BASIC_ID]}})
    remember_client_capabilities(app, session_id, caps)


# ---- round trip: create -> update components -> update data model -> delete ------


def test_producer_round_trip_through_the_store(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    create = build_create_a2ui_surface_tool()
    update_components = build_update_a2ui_components_tool()
    update_data_model = build_update_a2ui_data_model_tool()
    delete = build_delete_a2ui_surface_tool()

    created = create(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "First"}],
    )
    assert created.get("ok") is not False
    assert created["rendered"] is True
    assert created["created"] is True
    assert created["catalog_id"] == WORKSPACE_ID

    updated = update_components(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "Second"}],
    )
    assert updated["rendered"] is True
    assert updated["created"] is False
    assert updated["revision"] > created["revision"]

    data_modeled = update_data_model(surface_id="round-trip", path="/x", value=42)
    assert data_modeled["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "round-trip")
    assert surface is not None
    assert surface.messages[-1]["updateDataModel"] == {
        "surfaceId": "round-trip",
        "path": "/x",
        "value": 42,
    }

    deleted = delete(surface_id="round-trip")
    assert deleted["rendered"] is True
    assert deleted["deleted"] is True
    assert deleted["state"] == "deleted"

    # The tombstone is immediately visible to this session's own store (a
    # revision cannot resurrect a deleted surface without a new create).
    tombstoned = app.state.a2ui_store.get(sid, "round-trip")
    assert tombstoned is not None
    assert tombstoned.state == "deleted"
    refused = update_components(
        surface_id="round-trip",
        components=[{"id": "root", "component": "Text", "text": "Resurrected?"}],
    )
    assert refused["ok"] is False
    assert refused["reason"] == "a2ui_surface_not_found"


def test_inspect_surface_finds_prior_turn_definition(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    create = build_create_a2ui_surface_tool()
    inspect = build_inspect_a2ui_surface_tool()
    component = {"id": "root", "component": "Text", "text": "Original"}
    create(surface_id="recipe", components=[component])

    listed = inspect()
    assert listed["total"] == 1
    assert listed["surfaces"][0]["surface_id"] == "recipe"
    assert inspect(surface_id="recipe")["components"] == [component]
    assert inspect(surface_id="other")["reason"] == "a2ui_surface_not_found"


def test_update_data_model_delete_true_omits_the_value_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The protocol's own delete form: an omitted ``value`` key removes the path."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="deletable-key",
        components=[{"id": "root", "component": "Text", "text": "First"}],
        data_model={"ack": True},
    )

    result = build_update_a2ui_data_model_tool()(
        surface_id="deletable-key", path="/ack", delete=True
    )

    assert result["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "deletable-key")
    assert surface is not None
    last = surface.messages[-1]["updateDataModel"]
    assert last == {"surfaceId": "deletable-key", "path": "/ack"}
    assert "value" not in last


# ---- typed refusals, never exceptions ---------------------------------------------


def test_create_with_empty_catalog_id_and_no_advertisement_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)

    result = build_create_a2ui_surface_tool()(
        surface_id="unselectable",
        components=[{"id": "root", "component": "Text", "text": "First"}],
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_client_capabilities_unknown"
    assert "has not advertised" in result["detail"]
    assert result["hint"] == (
        "this session's client renders no A2UI catalogs; answer in prose, do not retry"
    )
    assert app.state.a2ui_store.get(sid, "unselectable") is None


def test_create_explicit_catalog_id_not_advertised_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """S4 adversarial-review fix: an explicit catalog_id for a NEW surface is
    a PREFERENCE, not a bypass -- it still must cross select_catalog's
    client-preference gate exactly like the empty-catalog_id default."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_basic_catalog_only(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="not-advertised",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_preferred_catalog_not_selectable"
    assert f"catalog_id {WORKSPACE_ID!r}" in result["detail"]
    assert "BOTH" in result["detail"]
    assert result["hint"] == (
        "omit catalog_id to auto-select instead, or pass one present in "
        "BOTH this result's client_supported_catalog_ids and "
        "producible_catalog_ids"
    )
    assert app.state.a2ui_store.get(sid, "not-advertised") is None


def test_create_explicit_catalog_id_advertised_is_honoured(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The positive twin: the same explicit catalog_id succeeds once it is
    both client-advertised and producible."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="advertised",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result.get("ok") is not False
    assert result["rendered"] is True
    assert result["catalog_id"] == WORKSPACE_ID


def test_component_validation_failure_names_the_load_skill_hint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="bad-button",
        components=[{"id": "root", "component": "Button"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert result["hint"] == (
        'load_skill("a2ui-catalog-clio-workspace", file="catalog.json#/components/Button")'
    )
    assert app.state.a2ui_store.get(sid, "bad-button") is None


def test_update_components_on_an_unknown_surface_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _session(tmp_path, monkeypatch)

    result = build_update_a2ui_components_tool()(
        surface_id="never-created",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result == {
        "ok": False,
        "reason": "a2ui_surface_not_found",
        "detail": "A2UI surface not found: never-created",
        "hint": (
            "reuse a live id from a prior result's session_surface_ids, or "
            "call create_a2ui_surface to make a new surface"
        ),
    }


def test_update_data_model_on_a_deleted_surface_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="retired",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )
    build_delete_a2ui_surface_tool()(surface_id="retired")

    result = build_update_a2ui_data_model_tool()(surface_id="retired", path="/x", value=1)

    assert result["ok"] is False
    assert result["reason"] == "a2ui_surface_not_found"


def test_delete_an_unknown_surface_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    _session(tmp_path, monkeypatch)

    result = build_delete_a2ui_surface_tool()(surface_id="ghost")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_surface_not_found"


def test_create_session_unavailable_is_a_typed_refusal(monkeypatch: Any) -> None:
    monkeypatch.setattr(gact_context, "active_app", lambda: None)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: "")

    result = build_create_a2ui_surface_tool()(
        surface_id="no-session", components=[{"id": "root", "component": "Text", "text": "x"}]
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_session_unavailable"


def test_create_missing_root_component_is_a_typed_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="no-root",
        components=[{"id": "not-root", "component": "Text", "text": "x"}],
        catalog_id=WORKSPACE_ID,
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert 'exactly one id="root"' in result["detail"]


# ---- S8: actionable refusal wording + repeated-refusal ledger (issue #1374) -------


def test_every_known_refusal_reason_has_a_nonempty_default_hint() -> None:
    """Completeness guard: a new refusal reason added without wording it in
    ``_DEFAULT_HINTS`` regresses back to the un-actionable-refusal bug
    #1374's live-gate comment fixed (14 identical retries in one turn)."""

    from clio_agent.gact.a2ui_producer._refusal import _DEFAULT_HINTS, KNOWN_REFUSAL_REASONS

    for reason in KNOWN_REFUSAL_REASONS:
        assert _DEFAULT_HINTS.get(reason), f"{reason} has no default hint"


def test_catalog_no_client_match_hint_says_answer_in_prose(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    from clio_agent.gact.a2ui_capabilities import remember_client_capabilities

    caps = A2UIClientCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": ["urn:example:unproducible/v1"]}}
    )
    remember_client_capabilities(app, sid, caps)

    result = build_create_a2ui_surface_tool()(
        surface_id="no-match",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_catalog_no_client_match"
    assert "urn:example:unproducible/v1" in result["detail"]
    assert result["hint"] == (
        "this session's client renders no catalog this session can produce; "
        "answer in prose, do not retry"
    )


def test_empty_client_advertisement_is_a_typed_refusal_never_a_fake_created(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """S1 A2UI catalog contract, item 3 (no-silent-fallback).

    A client that advertises ``supportedCatalogIds: []`` (gact-tui no longer
    falls back to its own well-known catalog ids when its registry route is
    unavailable -- ``processor-store.ts``) is a REAL advertisement, not a
    missing one: ``select_catalog`` must see ``caps is not None`` and fall
    through to ``a2ui_catalog_no_client_match`` on the empty intersection,
    exactly like a non-empty-but-unmatched list. Before the client-side fix,
    the client instead advertised its two well-known fallback ids, which
    happened to intersect this session's producible set, so the tool
    returned ``created: true`` for a catalog the client could not actually
    render -- the root cause of "Interactive surface unavailable" reported
    with a successful-looking tool result.
    """

    app, sid = _session(tmp_path, monkeypatch)
    from clio_schemas.a2ui.v0_9_1.capabilities import A2UIClientCapabilities

    from clio_agent.gact.a2ui_capabilities import remember_client_capabilities

    caps = A2UIClientCapabilities.model_validate({"v0.9": {"supportedCatalogIds": []}})
    remember_client_capabilities(app, sid, caps)

    result = build_create_a2ui_surface_tool()(
        surface_id="no-advertisement",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result["ok"] is False
    assert result.get("created") is not True
    assert result["reason"] == "a2ui_catalog_no_client_match"
    assert "supportedCatalogIds=[]" in result["detail"]
    assert result["hint"] == (
        "this session's client renders no catalog this session can produce; "
        "answer in prose, do not retry"
    )
    assert app.state.a2ui_store.get(sid, "no-advertisement") is None


def test_session_unavailable_hint_says_do_not_retry(monkeypatch: Any) -> None:
    monkeypatch.setattr(gact_context, "active_app", lambda: None)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: "")

    result = build_create_a2ui_surface_tool()(
        surface_id="no-session", components=[{"id": "root", "component": "Text", "text": "x"}]
    )

    assert result["reason"] == "a2ui_session_unavailable"
    assert "do not retry" in result["hint"]


def test_repeated_refusal_reason_in_one_turn_records_typed_ledger_reason(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The idle-cell evidence: create_a2ui_surface refused with the SAME
    reason 14 times in one turn, invisible without hand-reading a trace. A
    repeat within one turn now records ``a2ui_producer_refusal_repeated``,
    once per repeat, queryable via the registry's session ledger."""

    app, sid = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn-1")
    create = build_create_a2ui_surface_tool()

    first = create(surface_id="s1", components=[{"id": "root", "component": "Text", "text": "x"}])
    second = create(surface_id="s2", components=[{"id": "root", "component": "Text", "text": "x"}])
    third = create(surface_id="s3", components=[{"id": "root", "component": "Text", "text": "x"}])

    for result in (first, second, third):
        assert result["reason"] == "a2ui_client_capabilities_unknown"

    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    repeats = [r for r in reasons if r["reason"] == "a2ui_producer_refusal_repeated"]
    # First call is the original occurrence (no repeat yet); the second and
    # third calls are repeat #1 and #2.
    assert len(repeats) == 2
    assert [r["count"] for r in repeats] == [2, 3]
    assert all(r["refusal_reason"] == "a2ui_client_capabilities_unknown" for r in repeats)


def test_repeated_refusal_ledger_resets_across_turns(tmp_path: Path, monkeypatch: Any) -> None:
    """A NEW turn_id starts a fresh count -- a session that only ever sees one
    refusal per turn across many turns is never flagged as repeating."""

    app, sid = _session(tmp_path, monkeypatch)
    create = build_create_a2ui_surface_tool()

    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn-1")
    create(surface_id="s1", components=[{"id": "root", "component": "Text", "text": "x"}])
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn-2")
    create(surface_id="s2", components=[{"id": "root", "component": "Text", "text": "x"}])

    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    repeats = [r for r in reasons if r["reason"] == "a2ui_producer_refusal_repeated"]
    assert repeats == []


def test_repeated_refusal_ledger_is_per_reason_not_per_call(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two DIFFERENT refusal reasons in one turn are each a first occurrence,
    not a repeat of each other."""

    app, sid = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn-1")

    build_update_a2ui_components_tool()(
        surface_id="never-created", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    build_create_a2ui_surface_tool()(
        surface_id="s1", components=[{"id": "root", "component": "Text", "text": "x"}]
    )

    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    repeats = [r for r in reasons if r["reason"] == "a2ui_producer_refusal_repeated"]
    assert repeats == []


def test_repeated_refusal_with_no_active_turn_is_never_flagged_a_repeat(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """S8 review nit (issue #1374): ``turn_id=""`` (``gact/context.py``'s
    ``TurnContext`` default -- a producer tool called with no active turn,
    e.g. a script or a test harness) is not a real grouping key. Two
    out-of-turn refusals must never be treated as "the same turn,
    recurring", or the per-session state dict would accumulate counts
    across calls that have no actual temporal relationship."""

    app, sid = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "")

    for _ in range(3):
        build_create_a2ui_surface_tool()(
            surface_id="unselectable",
            components=[{"id": "root", "component": "Text", "text": "x"}],
        )

    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    repeats = [r for r in reasons if r["reason"] == "a2ui_producer_refusal_repeated"]
    assert repeats == []


def test_session_delete_prunes_the_producer_refusal_state(tmp_path: Path, monkeypatch: Any) -> None:
    """S8 review nit (issue #1374): ``CatalogRegistry.forget_session`` (called
    from ``DELETE /v1/sessions/{sid}``) drops this session's entry from
    every per-session ring the registry keeps, not just the reason ledger
    -- proven here by re-triggering the SAME reason after "delete" and
    confirming it counts as a FIRST occurrence again, not a leftover
    repeat from before the (simulated) delete."""

    app, sid = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn-1")

    build_create_a2ui_surface_tool()(
        surface_id="s1", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    build_create_a2ui_surface_tool()(
        surface_id="s2", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    before = app.state.a2ui_catalogs.session_reasons(sid)
    assert any(r["reason"] == "a2ui_producer_refusal_repeated" for r in before)

    app.state.a2ui_catalogs.forget_session(sid)
    app.state.a2ui_store.forget_session(sid)

    assert app.state.a2ui_catalogs.session_reasons(sid) == []
    build_create_a2ui_surface_tool()(
        surface_id="s3", components=[{"id": "root", "component": "Text", "text": "x"}]
    )
    after = app.state.a2ui_catalogs.session_reasons(sid)
    assert not any(r["reason"] == "a2ui_producer_refusal_repeated" for r in after), (
        "forget_session must reset the per-turn refusal count, not leave a stale one"
    )


# ---- docstrings carry no prop lore (S4 item 3) -------------------------------------


def test_producer_tool_docstrings_are_at_most_ten_lines_with_no_prop_lore() -> None:
    tools = [
        build_create_a2ui_surface_tool(),
        build_update_a2ui_components_tool(),
        build_update_a2ui_data_model_tool(),
        build_delete_a2ui_surface_tool(),
    ]
    # Deleted-docstring-wall content (a2ui_tools.py's 78 lines) named specific
    # component/property strings; none of that prose survives in the new tools.
    banned_substrings = (
        "clio.status.v1",
        "clio.map.v1",
        "clio.data-table.v1",
        "Tabs never use",
        "requires ``name``",
        "never accepts",
        "accessibility",
    )
    for tool in tools:
        doc = tool.func.__doc__ or ""
        lines = doc.strip().splitlines()
        assert len(lines) <= 10, f"{tool.name} docstring has {len(lines)} lines: {doc!r}"
        for banned in banned_substrings:
            assert banned not in doc, f"{tool.name} docstring still names {banned!r}"


def test_components_arg_description_says_data_uri_accepts_a_workspace_path() -> None:
    """#1533: a ``dataUri``/``url``/``uri`` value may be authored as a plain
    workspace path — the export boundary rewrites it to ``artifact://``
    automatically before validation. Stated in the ``components`` arg
    description (not the 10-line-capped docstring) for both tools."""

    for tool in (build_create_a2ui_surface_tool(), build_update_a2ui_components_tool()):
        description = tool.args["components"]["description"]
        assert "workspace" in description
        assert "artifact://" in description


def test_every_producer_tool_builds_in_the_surfaces_domain() -> None:
    """Merge regression: ``native_tool`` made ``domain`` keyword-required on the
    release line while three producer builders never passed it, so building the
    update/delete tools raised ``TypeError`` and only mypy noticed. Every
    producer tool must construct and carry the ``surfaces`` domain."""
    from clio_agent.gact.a2ui_producer import (
        build_create_a2ui_surface_tool,
        build_delete_a2ui_surface_tool,
        build_update_a2ui_components_tool,
        build_update_a2ui_data_model_tool,
    )
    from clio_agent.gact.agents.tool_instrumentation import DOMAIN_ATTR

    for build in (
        build_create_a2ui_surface_tool,
        build_update_a2ui_components_tool,
        build_update_a2ui_data_model_tool,
        build_delete_a2ui_surface_tool,
    ):
        tool = build()
        assert getattr(tool.func, DOMAIN_ATTR) == "surfaces", tool.name


# --------------------------------------------------------------------------- #
# components_path: an alternative to inline components (#1533 S4)
# --------------------------------------------------------------------------- #


def test_create_with_both_components_and_components_path_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    (tmp_path / "components.json").write_text(
        '[{"id": "root", "component": "Text", "text": "x"}]', encoding="utf-8"
    )

    result = build_create_a2ui_surface_tool()(
        surface_id="both",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        components_path="components.json",
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_source_conflict"
    assert app.state.a2ui_store.get(sid, "both") is None


def test_create_with_neither_components_nor_components_path_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(surface_id="neither")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_source_missing"


def test_create_with_components_path_reads_the_workspace_json_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    (tmp_path / "surface.json").write_text(
        '[{"id": "root", "component": "Text", "text": "from a file"}]', encoding="utf-8"
    )

    result = build_create_a2ui_surface_tool()(
        surface_id="from-path", components_path="surface.json"
    )

    assert result.get("ok") is not False, result
    assert result["rendered"] is True
    surface = app.state.a2ui_store.get(sid, "from-path")
    assert surface is not None
    components = surface.messages[-1]["updateComponents"]["components"]
    assert components == [{"id": "root", "component": "Text", "text": "from a file"}]


def test_components_path_outside_the_workspace_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    outside = tmp_path.parent / "outside.json"
    outside.write_text('[{"id": "root", "component": "Text", "text": "x"}]', encoding="utf-8")

    result = build_create_a2ui_surface_tool()(surface_id="escape", components_path=str(outside))

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_path_unresolved"


def test_components_path_naming_no_file_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="missing-file", components_path="does-not-exist.json"
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_path_unresolved"


def test_components_path_that_is_not_valid_json_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    (tmp_path / "broken.json").write_text("not json {", encoding="utf-8")

    result = build_create_a2ui_surface_tool()(surface_id="broken", components_path="broken.json")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_path_invalid"


def test_components_path_that_is_not_an_array_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    (tmp_path / "object.json").write_text(
        '{"id": "root", "component": "Text", "text": "x"}', encoding="utf-8"
    )

    result = build_create_a2ui_surface_tool()(surface_id="not-array", components_path="object.json")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_path_invalid"


def test_components_path_with_a_non_object_entry_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """#1533 S4 adversarial review item 7: an array is not enough -- every
    entry must itself be a component OBJECT, or this is a typed refusal
    naming which index broke, never a downstream crash trying to treat a
    string/number/null as a component dict."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    (tmp_path / "mixed.json").write_text(
        '[{"id": "root", "component": "Text", "text": "x"}, "not-an-object", 42]',
        encoding="utf-8",
    )

    result = build_create_a2ui_surface_tool()(surface_id="mixed", components_path="mixed.json")

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_path_invalid"
    assert "[1, 2]" in result["detail"]


def test_update_components_also_supports_components_path(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="upd", components=[{"id": "root", "component": "Text", "text": "first"}]
    )
    (tmp_path / "update.json").write_text(
        '[{"id": "root", "component": "Text", "text": "second"}]', encoding="utf-8"
    )

    result = build_update_a2ui_components_tool()(surface_id="upd", components_path="update.json")

    assert result.get("ok") is not False, result
    surface = app.state.a2ui_store.get(sid, "upd")
    assert surface is not None
    components = surface.messages[-1]["updateComponents"]["components"]
    assert components == [{"id": "root", "component": "Text", "text": "second"}]


def test_update_components_with_both_components_and_components_path_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="upd2", components=[{"id": "root", "component": "Text", "text": "first"}]
    )

    result = build_update_a2ui_components_tool()(
        surface_id="upd2",
        components=[{"id": "root", "component": "Text", "text": "x"}],
        components_path="anything.json",
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_components_source_conflict"


# --------------------------------------------------------------------------- #
# Every surface keeps its definition: a minted artifact linked to it (#1533 S4)
# --------------------------------------------------------------------------- #


def test_create_a2ui_surface_mints_a_definition_artifact(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="defined",
        components=[{"id": "root", "component": "Text", "text": "hello"}],
    )

    assert result.get("ok") is not False, result
    assert result["definition_artifact_id"].startswith("artifact_")
    assert result["definition_artifact_uri"] == f"artifact://{result['definition_artifact_id']}"

    record, version = app.state.artifact_registry.get_by_artifact_id(
        result["definition_artifact_id"]
    )
    assert record.name == "defined.json"
    assert version.producer["designation"] == "a2ui-surface-definition"
    assert version.producer["surface_id"] == "defined"

    stored_path = Path(version.path)
    assert stored_path.is_file()
    assert json.loads(stored_path.read_text(encoding="utf-8")) == [
        {"id": "root", "component": "Text", "text": "hello"}
    ]


@pytest.mark.parametrize(
    "malicious_surface_id",
    [
        "../../../evil",
        "../../evil",
        "..",
        "../outside",
        "/etc/passwd",
        "C:\\Windows\\evil",
        "a/../../b",
        "a\\..\\..\\b",
    ],
)
def test_definition_artifact_never_escapes_the_workspace(
    tmp_path: Path, monkeypatch: Any, malicious_surface_id: str
) -> None:
    """CRITICAL adversarial review defect: surface_id (a raw model-authored
    string) was joined directly into the definition artifact's path
    (``.clio/a2ui/{surface_id}.json``), so a value like ``../../../evil``
    escaped the workspace entirely. The surface still creates successfully
    (surface_id is never rejected outright); its definition artifact is
    stored under a HASHED, safe name instead, always inside the workspace."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    outside_marker = tmp_path.parent / "evil.json"
    outside_marker_existed_before = outside_marker.exists()

    result = build_create_a2ui_surface_tool()(
        surface_id=malicious_surface_id,
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result.get("ok") is not False, result
    artifact_id = result["definition_artifact_id"]
    _record, version = app.state.artifact_registry.get_by_artifact_id(artifact_id)
    stored_path = Path(version.path).resolve()

    # The stored file is INSIDE the workspace root, never outside it.
    assert stored_path.is_relative_to(tmp_path.resolve())
    # Nothing was ever written at any traversal-escaped location.
    assert outside_marker.exists() == outside_marker_existed_before
    for evil_path in (
        tmp_path.parent / "evil.json",
        Path(malicious_surface_id.replace("\\", "/") + ".json"),
    ):
        if not str(evil_path).startswith(str(tmp_path)):
            assert not evil_path.is_file()


def test_definition_artifact_filename_is_hashed_for_a_traversal_surface_id(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    result = build_create_a2ui_surface_tool()(
        surface_id="../../../evil",
        components=[{"id": "root", "component": "Text", "text": "x"}],
    )

    assert result.get("ok") is not False, result
    _record, version = app.state.artifact_registry.get_by_artifact_id(
        result["definition_artifact_id"]
    )
    stored_path = Path(version.path)
    assert stored_path.parent == (tmp_path / ".clio" / "a2ui").resolve()
    assert stored_path.name.startswith("surface-")
    assert stored_path.name != "evil.json"


def test_definition_artifact_filename_is_stable_for_the_same_traversal_surface_id(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Re-applying the SAME (malicious) surface_id still dedups/version-chains
    onto the same logical artifact -- the hash is a function of surface_id,
    not randomized per call."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)

    first = build_create_a2ui_surface_tool()(
        surface_id="../../../evil",
        components=[{"id": "root", "component": "Text", "text": "v1"}],
    )
    second = build_update_a2ui_components_tool()(
        surface_id="../../../evil",
        components=[{"id": "root", "component": "Text", "text": "v2"}],
    )

    assert first.get("ok") is not False, first
    assert second.get("ok") is not False, second
    first_record, _ = app.state.artifact_registry.get_by_artifact_id(
        first["definition_artifact_id"]
    )
    second_record, _ = app.state.artifact_registry.get_by_artifact_id(
        second["definition_artifact_id"]
    )
    assert first_record.name == second_record.name


def test_definition_file_name_allows_a_safe_surface_id_verbatim() -> None:
    from clio_agent.gact.a2ui_producer._definition_artifact import _definition_file_name

    assert _definition_file_name("my-surface_1") == "my-surface_1.json"


@pytest.mark.parametrize(
    "unsafe",
    ["../evil", "a/b", "a\\b", "C:\\evil", "..", "", "a.b", "a b"],
)
def test_definition_file_name_hashes_anything_outside_the_safe_charset(unsafe: str) -> None:
    from clio_agent.gact.a2ui_producer._definition_artifact import _definition_file_name

    name = _definition_file_name(unsafe)
    assert "/" not in name
    assert "\\" not in name
    assert ".." not in name
    assert name.startswith("surface-")
    assert name.endswith(".json")


def test_updating_a_surfaces_components_remints_its_definition_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    first = build_create_a2ui_surface_tool()(
        surface_id="redefine",
        components=[{"id": "root", "component": "Text", "text": "v1"}],
    )

    second = build_update_a2ui_components_tool()(
        surface_id="redefine",
        components=[{"id": "root", "component": "Text", "text": "v2"}],
    )

    assert first["definition_artifact_id"] != second["definition_artifact_id"]
    _record, version = app.state.artifact_registry.get_by_artifact_id(
        second["definition_artifact_id"]
    )
    assert version.version == 2


def test_reapplying_the_same_components_dedups_onto_the_existing_definition_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    same_components = [{"id": "root", "component": "Text", "text": "unchanged"}]
    first = build_create_a2ui_surface_tool()(surface_id="stable", components=same_components)

    second = build_update_a2ui_components_tool()(
        surface_id="stable", components=list(same_components)
    )

    assert first["definition_artifact_id"] == second["definition_artifact_id"]


def test_partial_update_definition_artifact_carries_the_full_merged_surface(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MEDIUM adversarial review defect: update_a2ui_components upserts a
    SUBSET of a multi-component surface, but the definition artifact used
    to be minted from just that subset -- losing every OTHER component the
    surface actually has. It must always record the full, live surface."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="multi",
        components=[
            {"id": "root", "component": "Column", "children": ["title"]},
            {"id": "title", "component": "Text", "text": "Hello"},
        ],
    )

    result = build_update_a2ui_components_tool()(
        surface_id="multi",
        components=[{"id": "title", "component": "Text", "text": "Updated"}],
    )

    assert result.get("ok") is not False, result
    _record, version = app.state.artifact_registry.get_by_artifact_id(
        result["definition_artifact_id"]
    )
    stored = json.loads(Path(version.path).read_text(encoding="utf-8"))
    assert stored == [
        {"id": "root", "component": "Column", "children": ["title"]},
        {"id": "title", "component": "Text", "text": "Updated"},
    ]


def test_create_rejects_an_unattached_chart_instead_of_claiming_it_rendered(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    result = build_create_a2ui_surface_tool()(
        surface_id="unattached",
        components=[
            {"id": "root", "component": "Text", "text": "Map"},
            {"id": "scatter", "component": "Text", "text": "Chart"},
        ],
    )

    assert result["ok"] is False
    assert result["reason"] == "a2ui_validation_failed"
    assert "scatter" in result["detail"]
    assert "root Row, Column, Grid" in result["detail"]
    assert app.state.a2ui_store.get(sid, "unattached") is None


def test_create_accepts_a_layout_referencing_every_child(tmp_path: Path, monkeypatch: Any) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    result = build_create_a2ui_surface_tool()(
        surface_id="composed",
        components=[
            {"id": "root", "component": "Row", "children": ["map", "scatter"]},
            {"id": "map", "component": "Text", "text": "Map"},
            {"id": "scatter", "component": "Text", "text": "Chart"},
        ],
    )

    assert result.get("ok") is not False, result
    assert app.state.a2ui_store.get(sid, "composed") is not None


def test_partial_update_adding_a_new_component_appends_to_the_merged_definition(
    tmp_path: Path, monkeypatch: Any
) -> None:
    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    build_create_a2ui_surface_tool()(
        surface_id="growing",
        components=[{"id": "root", "component": "Column", "children": []}],
    )

    result = build_update_a2ui_components_tool()(
        surface_id="growing",
        components=[
            {"id": "root", "component": "Column", "children": ["extra"]},
            {"id": "extra", "component": "Text", "text": "New"},
        ],
    )

    assert result.get("ok") is not False, result
    _record, version = app.state.artifact_registry.get_by_artifact_id(
        result["definition_artifact_id"]
    )
    stored = json.loads(Path(version.path).read_text(encoding="utf-8"))
    assert stored == [
        {"id": "root", "component": "Column", "children": ["extra"]},
        {"id": "extra", "component": "Text", "text": "New"},
    ]


def test_a_refused_update_never_mints_or_overwrites_the_definition_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """MEDIUM adversarial review defect: a refused (schema-invalid) batch
    must not mint a new definition artifact -- nothing was actually applied
    to the surface, so its recorded definition must not change either."""

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    created = build_create_a2ui_surface_tool()(
        surface_id="guarded",
        components=[{"id": "root", "component": "Text", "text": "v1"}],
    )
    original_definition_id = created["definition_artifact_id"]

    refused = build_update_a2ui_components_tool()(
        surface_id="guarded",
        # Button with no required "child"/"action" -- fails catalog schema
        # validation inside apply_messages.
        components=[{"id": "root", "component": "Button"}],
    )

    assert refused["ok"] is False
    assert refused["reason"] == "a2ui_validation_failed"
    assert "definition_artifact_id" not in refused

    surface = app.state.a2ui_store.get(sid, "guarded")
    assert surface is not None
    assert surface.messages[-1]["updateComponents"]["components"] == [
        {"id": "root", "component": "Text", "text": "v1"}
    ]
    _record, version = app.state.artifact_registry.get_by_artifact_id(original_definition_id)
    assert version.version == 1


def test_new_components_path_and_definition_reasons_have_default_hints() -> None:
    from clio_agent.gact.a2ui_producer._refusal import _DEFAULT_HINTS, KNOWN_REFUSAL_REASONS

    for reason in (
        "a2ui_components_source_conflict",
        "a2ui_components_source_missing",
        "a2ui_components_path_unresolved",
        "a2ui_components_path_invalid",
        "a2ui_definition_artifact_failed",
    ):
        assert reason in KNOWN_REFUSAL_REASONS
        assert _DEFAULT_HINTS.get(reason)


def test_component_limit_exceeded_has_an_actionable_hint_and_is_recorded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """M3 (coordinator design, adversarial re-review): F5's merged-component
    cap refusal must carry a concrete, actionable hint -- not the
    un-actionable-retry pattern issue #1374 fixed -- and must land in the
    session's own typed reason ledger, the same way its catalog-error
    siblings (``a2ui_catalog_unknown``/``a2ui_catalog_not_producible``) do.
    """

    import clio_agent.gact.a2ui as a2ui_module
    from clio_agent.gact.a2ui_producer._refusal import _DEFAULT_HINTS, KNOWN_REFUSAL_REASONS

    monkeypatch.setattr(a2ui_module, "MAX_A2UI_COMPONENTS", 2)

    app, sid = _session(tmp_path, monkeypatch)
    _advertise_workspace_catalog(app, sid)
    create = build_create_a2ui_surface_tool()
    update_components = build_update_a2ui_components_tool()

    created = create(
        surface_id="capped",
        components=[{"id": "root", "component": "Text", "text": "one"}],
    )
    assert created["rendered"] is True

    # "root" (1) + "overflow" (1) = 2, AT the cap -- still fine.
    at_cap = update_components(
        surface_id="capped",
        components=[{"id": "overflow", "component": "Text", "text": "two"}],
    )
    assert at_cap["rendered"] is True

    refused = update_components(
        surface_id="capped",
        components=[{"id": "overflow_2", "component": "Text", "text": "three"}],
    )

    assert refused["ok"] is False
    assert refused["reason"] == "a2ui_component_limit_exceeded"
    assert refused["reason"] in KNOWN_REFUSAL_REASONS
    assert refused["hint"] == _DEFAULT_HINTS["a2ui_component_limit_exceeded"]
    # Actionable: names a concrete next step, never a bare retry.
    assert "reuse" in refused["hint"] or "delete_a2ui_surface" in refused["hint"]

    reasons = app.state.a2ui_catalogs.session_reasons(sid)
    recorded = [r for r in reasons if r["reason"] == "a2ui_component_limit_exceeded"]
    assert len(recorded) == 1
    assert recorded[0]["component_count"] == 3
    assert recorded[0]["limit"] == 2
