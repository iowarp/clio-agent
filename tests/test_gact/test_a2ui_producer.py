"""A2UI producer tools (S4, docs/design/a2ui-compat-campaign-2026-09.md).

Round-trips the four producer tools (create/update-components/update-data-
model/delete) through the transcript-owned surface store, and proves every
producer mistake comes back as a typed refusal dict, never an exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact import context as gact_context
from clio_agent.gact.a2ui_capabilities import remember_client_capabilities
from clio_agent.gact.a2ui_catalogs.builtin import basic_catalog_id, workspace_catalog_id
from clio_agent.gact.a2ui_producer import (
    build_create_a2ui_surface_tool,
    build_delete_a2ui_surface_tool,
    build_update_a2ui_components_tool,
    build_update_a2ui_data_model_tool,
)
from clio_agent.gact.app import build_app

WORKSPACE_ID = workspace_catalog_id()
BASIC_ID = basic_catalog_id()


def _session(tmp_path: Path, monkeypatch: Any) -> tuple[Any, str]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
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
