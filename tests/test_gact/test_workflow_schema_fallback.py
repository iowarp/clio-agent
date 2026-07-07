"""Slice E — loud, queryable workflow_state generic fallback + hard load error.

The pack-declared workflow_state seam degrades to the GENERIC (presence-only)
engine whenever a session's active Agent Blueprint declares no schema. That
degradation MUST NOT be silent (no-silent-fallback ground rule): the resolver
records a structured ``workflow_state_schema_absent`` reason in a dedicated
per-app ledger, and a *malformed* declaration is rejected at blueprint load
(the blueprint is disabled) so it never reaches the resolver.

These tests prove, for the owner seam
(:func:`clio_agent.gact.agents.resolution._active_workflow_state_schema`) and the
sibling catalog (:mod:`clio_agent.gact.streaming`):

* **bool-only pack** — a blueprint with ``workflow_state: true`` and no schema
  resolves GENERIC AND records exactly one catalog reason (the cache dedupes
  repeated resolutions);
* **app/session absent** — nothing to attribute, so GENERIC with no record;
* **malformed declaration** — the blueprint loads disabled with an
  ``invalid workflow_state schema`` validation error;
* **reject-unknowns** — the payload builder rejects an unknown reason;
* **closed set intact** — the reason is NOT in the audited client-facing
  stream_fallback capability set.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root
from clio_agent.gact.agents import resolution
from clio_agent.gact.agents.resolution import _active_workflow_state_schema
from clio_agent.gact.runtime.capabilities import _stream_fallback_reason_capabilities
from clio_agent.gact.streaming import (
    _WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS,
    _workflow_schema_fallback_payload,
    _workflow_schema_fallbacks,
)
from clio_agent.gact.workflow_state.schema import GENERIC_WORKFLOW_STATE_SCHEMA


def _app() -> Any:
    """A minimal FastAPI-shaped stub carrying only ``.state`` (an attr bag)."""
    return SimpleNamespace(state=SimpleNamespace())


def _pin_active_blueprint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    blueprint_id: str,
    metadata: dict[str, Any],
    enabled: bool = True,
) -> None:
    """Pin the resolver's blueprint-identity seam to a synthetic blueprint.

    Routes resolution through the ``blueprint_path`` branch (avoiding on-disk
    discovery) so the test controls exactly the declared ``workflow_state``.
    """
    monkeypatch.setattr(
        resolution, "_runtime_active_agent_blueprint_id", lambda app, session_id="": blueprint_id
    )
    monkeypatch.setattr(
        resolution,
        "_runtime_active_agent_blueprint_path",
        lambda app, session_id="": Path("/synthetic/AGENT.md"),
    )
    monkeypatch.setattr(
        resolution,
        "parse_agent_blueprint_root",
        lambda root, scope="session": SimpleNamespace(enabled=enabled, metadata=metadata),
    )


def test_bool_only_pack_resolves_generic_and_records_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``workflow_state: true`` (bool-only) blueprint resolves GENERIC and records
    exactly one ``workflow_state_schema_absent`` reason — the cache dedupes repeats."""
    app = _app()
    _pin_active_blueprint(monkeypatch, blueprint_id="bp-boolonly", metadata={"workflow_state": True})

    first = _active_workflow_state_schema(app, "sess-1")
    second = _active_workflow_state_schema(app, "sess-1")  # cache hit, no second record

    assert first is GENERIC_WORKFLOW_STATE_SCHEMA
    assert second is GENERIC_WORKFLOW_STATE_SCHEMA
    entries = _workflow_schema_fallbacks(app)["sess-1"]
    assert len(entries) == 1
    payload = entries[0]
    assert payload["reason"] == "workflow_state_schema_absent"
    assert payload["category"] == "pack_declaration"
    assert payload["recovery_actions"]
    assert "bp-boolonly" in payload["message"]


def test_absent_app_or_session_resolves_generic_without_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No app / no session id -> GENERIC and NOTHING recorded (nothing to attribute)."""
    # App-less: the early return fires before any ledger touch.
    assert _active_workflow_state_schema(None, "sess-1") is GENERIC_WORKFLOW_STATE_SCHEMA

    # An app but no session id: GENERIC, and the ledger stays empty.
    app = _app()
    assert _active_workflow_state_schema(app, "") is GENERIC_WORKFLOW_STATE_SCHEMA
    assert _workflow_schema_fallbacks(app) == {}


def test_malformed_declaration_disables_blueprint_at_load(tmp_path: Path) -> None:
    """A blueprint whose ``workflow_state`` does not compile is disabled at load with
    an ``invalid workflow_state schema`` validation error (fail loud at the boundary)."""
    root = tmp_path / "bad-blueprint"
    root.mkdir()
    (root / "AGENT.md").write_text(
        "\n".join(
            [
                "---",
                "id: bad-bp",
                "workflow_state:",
                "  sections:",
                "    acquisition:",
                '      status_ranks: "nope"',
                "---",
                "body",
                "",
            ]
        ),
        encoding="utf-8",
    )

    blueprint = parse_agent_blueprint_root(root, scope="session")

    assert blueprint.enabled is False
    assert any("invalid workflow_state schema" in err for err in blueprint.validation_errors)
    # The raw declaration is still stamped verbatim for provenance display.
    assert isinstance(blueprint.metadata.get("workflow_state"), dict)


def test_payload_rejects_unknown_reason() -> None:
    """Like the stream_fallback payload, an unknown reason is rejected (no bare fallback)."""
    with pytest.raises(ValueError, match="Unknown workflow_state schema fallback reason"):
        _workflow_schema_fallback_payload("not_a_real_reason")


def test_reason_absent_from_audited_streaming_capability_set() -> None:
    """The workflow_state reason lives in its dedicated sibling catalog, NOT the
    audited client-facing stream_fallback capability set (a closed live-streaming set)."""
    assert "workflow_state_schema_absent" in _WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS
    assert "workflow_state_schema_absent" not in _stream_fallback_reason_capabilities()
