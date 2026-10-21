"""Slice F — the EarthScope workflow_state schema ships in the installed plugin.

Phase C extracted the EarthScope ``workflow_state`` vocabulary out of a core
hardcode; it now lives in the marketplace's shared ``earthscope`` plugin, as the
``ai.iowarp.clio.workflow_state`` extension of its ``plugin.json`` (marketplace
submodule, pinned here). These end-to-end tests close the loop the Slice-B
goldens opened:

* **plugin declaration == fixture** — loading the *shipped* ``earthscope`` plugin
  and validating its ``workflow_state`` declaration yields EXACTLY the
  :data:`EARTHSCOPE_WORKFLOW_STATE_SCHEMA` fixture that every Slice-B golden
  pins the engine against. The shipped declaration and the engine can never
  drift.
* **resolver returns the typed schema** — a session of the shipped
  ``earthscope-gnss-region`` agent resolves through
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
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA

pytestmark = pytest.mark.marketplace

_EARTHSCOPE_PLUGIN = MARKETPLACE_ROOT / "plugins" / "earthscope"


def test_earthscope_plugin_declares_workflow_state_schema() -> None:
    """The shipped plugin declares a ``workflow_state`` vocabulary that validates to
    EXACTLY the transcribed EARTHSCOPE fixture.

    This is the end-to-end re-assertion of the Slice-B pin: the fixture every
    golden merges/grounds/scrubs against is proven identical to the schema the
    shipped plugin declares — the declaration and the engine cannot drift.
    """
    record = load_plugin(_EARTHSCOPE_PLUGIN, marketplace="clio", trusted=True)

    assert record.valid, [row.to_dict() for row in record.errors]
    declaration = record.workflow_state
    assert isinstance(declaration, dict)
    assert WorkflowStateSchema.model_validate(declaration) == EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def test_resolver_returns_typed_schema_for_earthscope_session_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session of the *shipped* EarthScope GNSS region agent resolves the typed
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
    resolution = prepare_session_agent(app, metadata, "clio/earthscope-gnss-region")
    assert "earthscope" in [row["id"] for row in resolution.plugins]
    sid = app.state.sessions.create(workspace_id="ws_default", metadata=metadata).id

    schema = session_workflow_state_schema(app, sid)

    assert schema is not GENERIC_WORKFLOW_STATE_SCHEMA
    assert schema == EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    # No degradation: the declaration is present, so nothing is recorded.
    assert _workflow_schema_fallbacks(app) == {}
