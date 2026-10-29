"""Slice F — the EarthScope workflow_state schema ships in the installed plugin.

Phase C extracted the EarthScope ``workflow_state`` vocabulary out of a core
hardcode; it now lives in the marketplace's ``earthscope`` plugin, as the
``ai.iowarp.clio.workflow_state`` extension of its ``plugin.json`` (marketplace
submodule, pinned here). These end-to-end tests check what ships:

* **typed declaration** — the *shipped* ``earthscope`` plugin declares a
  ``workflow_state`` vocabulary with named sections (never the GENERIC dict).
  The Slice-B goldens pin the engine against their own fixture
  (``tests/test_gact/earthscope_schema.py``), independent of any plugin.
* **resolver returns the typed schema** — a session of the shipped
  ``earthscope`` agent resolves through
  :func:`~clio_agent.gact.agents.resolution.session_workflow_state_schema` to
  the typed (non-GENERIC) engine, and records NO
  ``workflow_state_schema_absent`` fallback (the declaration is present, so the
  no-silent-fallback path stays quiet).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents.resolution import prepare_session_agent, session_workflow_state_schema
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.streaming import _workflow_schema_fallbacks
from clio_agent.gact.workflow_state.schema import (
    GENERIC_WORKFLOW_STATE_SCHEMA,
    WorkflowStateSchema,
)
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.service import PluginService
from tests._marketplace import MARKETPLACE_ROOT

pytestmark = pytest.mark.marketplace

_EARTHSCOPE_PLUGIN = MARKETPLACE_ROOT / "plugins" / "earthscope"


def _shipped_schema() -> WorkflowStateSchema:
    record = load_plugin(_EARTHSCOPE_PLUGIN, marketplace="clio", trusted=True)
    assert record.valid, [row.to_dict() for row in record.errors]
    assert isinstance(record.workflow_state, dict)
    return WorkflowStateSchema.model_validate(record.workflow_state)


def test_earthscope_plugin_declares_a_typed_workflow_state_schema() -> None:
    """The shipped plugin declares a ``workflow_state`` vocabulary with named sections:
    a typed engine, never the GENERIC free dict."""

    schema = _shipped_schema()
    assert schema.sections
    assert schema is not GENERIC_WORKFLOW_STATE_SCHEMA


def test_resolver_returns_typed_schema_for_earthscope_session_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session of the *shipped* EarthScope agent resolves the typed
    schema (not GENERIC) and records no fallback.

    The shipped marketplace is the default marketplace here and the session pins the
    agent the way session creation does; the declaration is present, so the typed
    engine is returned and the ``workflow_state_schema_absent`` ledger stays empty
    (the loud generic-fallback path must NOT fire).
    """
    # A snapshot of the shipped marketplace without local build output (plugin
    # ``impl/.venv`` trees a concurrent server build may be writing).
    snapshot = tmp_path / "marketplace"
    shutil.copytree(
        MARKETPLACE_ROOT,
        snapshot,
        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__"),
    )
    monkeypatch.setenv("CLIO_DEFAULT_MARKETPLACE", str(snapshot))
    app: Any = SimpleNamespace(
        state=SimpleNamespace(
            plugin_service=PluginService(), sessions=SessionStore(path=tmp_path / "sessions.json")
        )
    )
    metadata: dict[str, Any] = {}
    resolution = prepare_session_agent(app, metadata, "clio/earthscope")
    assert "earthscope" in [row["id"] for row in resolution.plugins]
    sid = app.state.sessions.create(workspace_id="ws_default", metadata=metadata).id

    schema = session_workflow_state_schema(app, sid)

    assert schema is not GENERIC_WORKFLOW_STATE_SCHEMA
    assert schema == _shipped_schema()
    # No degradation: the declaration is present, so nothing is recorded.
    assert _workflow_schema_fallbacks(app) == {}
