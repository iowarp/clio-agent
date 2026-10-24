"""Slice E — loud, queryable workflow_state generic fallback + hard load error.

A plugin declares the typed ``workflow_state`` vocabulary in its ``plugin.json``
extension (``ai.iowarp.clio.workflow_state``). A session whose resolved plugins
declare none degrades to the GENERIC (presence-only) engine. That degradation
MUST NOT be silent (no-silent-fallback ground rule): the resolver records a
structured ``workflow_state_schema_absent`` reason in a dedicated per-app
ledger, and a *malformed* declaration is rejected when the plugin loads so it
never reaches the resolver.

These tests prove, for the owner seam
(:func:`clio_agent.gact.agents.resolution.session_workflow_state_schema`) and the
sibling catalog (:mod:`clio_agent.gact.streaming`):

* **no declaring plugin** — a session of an agent whose plugins declare no schema
  resolves GENERIC AND records exactly one catalog reason (the cache dedupes
  repeated resolutions);
* **declaring plugin** — the typed schema, nothing recorded;
* **app/session absent** — nothing to attribute, so GENERIC with no record;
* **malformed declaration** — the plugin loads with an error diagnostic naming
  the ``workflow_state`` declaration;
* **reject-unknowns** — the payload builder rejects an unknown reason;
* **closed set intact** — the reason is NOT in the audited client-facing
  stream_fallback capability set.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.resolution import prepare_session_agent, session_workflow_state_schema
from clio_agent.gact.runtime.capabilities import _stream_fallback_reason_capabilities
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.streaming import (
    _WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS,
    _workflow_schema_fallback_payload,
    _workflow_schema_fallbacks,
)
from clio_agent.gact.workflow_state.schema import GENERIC_WORKFLOW_STATE_SCHEMA
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.service import PluginService
from tests._plugin_fixtures import custom_marketplace
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA
from tests.test_plugins.helpers import plugin

STATEFUL_AGENT = """
from clio_agent.sdk import Agent


class Stateful(Agent):
    id = "stateful"
    version = "1.0.0"
    name = "Stateful"
    role = "a GNSS region assistant"
    description = "Carries a plugin that declares a workflow_state vocabulary."
    plugins = Agent.plugins + ["gnss-state"]
"""


def _app() -> Any:
    """A FastAPI-shaped stub carrying the plugin service and a session store."""
    return SimpleNamespace(state=SimpleNamespace(plugin_service=PluginService()))


def _session(app: Any, tmp_path: Path, ref: str) -> str:
    """Create a session pinned to ``ref``, as session creation pins it."""
    if getattr(app.state, "sessions", None) is None:
        app.state.sessions = SessionStore(path=tmp_path / "sessions.json")
    metadata: dict[str, Any] = {}
    prepare_session_agent(app, metadata, ref)
    return app.state.sessions.create(workspace_id="ws_default", metadata=metadata).id


def test_agent_without_a_declaring_plugin_resolves_generic_and_records_once(
    tmp_path: Path,
) -> None:
    """No resolved plugin declares a vocabulary: GENERIC, and exactly one
    ``workflow_state_schema_absent`` reason — the cache dedupes repeats."""
    app = _app()
    sid = _session(app, tmp_path, "clio/standard")

    first = session_workflow_state_schema(app, sid)
    second = session_workflow_state_schema(app, sid)  # cache hit, no second record

    assert first is GENERIC_WORKFLOW_STATE_SCHEMA
    assert second is GENERIC_WORKFLOW_STATE_SCHEMA
    entries = _workflow_schema_fallbacks(app)[sid]
    assert len(entries) == 1
    payload = entries[0]
    assert payload["reason"] == "workflow_state_schema_absent"
    assert payload["category"] == "pack_declaration"
    assert payload["recovery_actions"]
    assert "clio/standard" in payload["message"]


def test_a_declaring_plugin_gives_the_typed_schema_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin's ``workflow_state`` declaration reaches the session as the typed engine,
    and the loud generic-fallback path stays quiet."""
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "gnss-state": {
                "ext": {"workflow_state": EARTHSCOPE_WORKFLOW_STATE_SCHEMA.model_dump(mode="json")}
            }
        },
        agents={"stateful.py": STATEFUL_AGENT},
    )
    app = _app()
    sid = _session(app, tmp_path, "clio/stateful")

    schema = session_workflow_state_schema(app, sid)

    assert schema is not GENERIC_WORKFLOW_STATE_SCHEMA
    assert schema == EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    assert _workflow_schema_fallbacks(app) == {}


def test_absent_app_or_session_resolves_generic_without_recording() -> None:
    """No app / no session id -> GENERIC and NOTHING recorded (nothing to attribute)."""
    # App-less: the early return fires before any ledger touch.
    assert session_workflow_state_schema(None, "sess-1") is GENERIC_WORKFLOW_STATE_SCHEMA

    # An app but no session id: GENERIC, and the ledger stays empty.
    app = _app()
    assert session_workflow_state_schema(app, "") is GENERIC_WORKFLOW_STATE_SCHEMA
    assert _workflow_schema_fallbacks(app) == {}


def test_malformed_declaration_is_rejected_at_plugin_load(tmp_path: Path) -> None:
    """A plugin whose ``workflow_state`` does not compile is rejected when it loads, with
    an error diagnostic naming the declaration (fail loud at the boundary)."""
    root = plugin(
        tmp_path,
        "bad-state",
        ext={"workflow_state": {"sections": {"acquisition": {"status_ranks": "nope"}}}},
    )

    record = load_plugin(root, trusted=True)

    assert not record.valid
    assert any("workflow_state" in row.message for row in record.errors)


def test_payload_rejects_unknown_reason() -> None:
    """Like the stream_fallback payload, an unknown reason is rejected (no bare fallback)."""
    with pytest.raises(ValueError, match="Unknown workflow_state schema fallback reason"):
        _workflow_schema_fallback_payload("not_a_real_reason")


def test_reason_absent_from_audited_streaming_capability_set() -> None:
    """The workflow_state reason lives in its dedicated sibling catalog, NOT the
    audited client-facing stream_fallback capability set (a closed live-streaming set)."""
    assert "workflow_state_schema_absent" in _WORKFLOW_SCHEMA_FALLBACK_REASON_DEFINITIONS
    assert "workflow_state_schema_absent" not in _stream_fallback_reason_capabilities()
