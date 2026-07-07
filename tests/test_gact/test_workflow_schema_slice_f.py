"""Slice F — the EarthScope workflow_state schema ships in the installed pack.

Phase C extracts the EarthScope ``workflow_state`` vocabulary out of a core
hardcode and into the ``earthscope-gnss-region`` Agent Blueprint's AGENT.md
frontmatter (marketplace submodule, pinned here). These end-to-end tests close
the loop the Slice-B goldens opened:

* **pack declaration == fixture** — parsing the *installed* pack's AGENT.md
  frontmatter and validating its ``workflow_state`` block yields EXACTLY the
  :data:`EARTHSCOPE_WORKFLOW_STATE_SCHEMA` fixture that every Slice-B golden
  pins the engine against. The shipped declaration and the engine can never
  drift.
* **resolver returns the typed schema** — a session whose active blueprint is
  the installed EarthScope pack resolves through
  :func:`~clio_agent.gact.agents.resolution._active_workflow_state_schema` to
  the typed (non-GENERIC) engine, and records NO
  ``workflow_state_schema_absent`` fallback (the declaration is present, so the
  no-silent-fallback path stays quiet).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agents import resolution
from clio_agent.gact.agents.resolution import _active_workflow_state_schema
from clio_agent.gact.expert_packs import _parse_frontmatter
from clio_agent.gact.streaming import _workflow_schema_fallbacks
from clio_agent.gact.workflow_state.schema import (
    GENERIC_WORKFLOW_STATE_SCHEMA,
    WorkflowStateSchema,
)
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA

_EARTHSCOPE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "external"
    / "clio-agent-marketplace"
    / "earthscope-gnss-region"
)


def test_earthscope_pack_declares_workflow_state_schema() -> None:
    """The installed pack's AGENT.md frontmatter declares a ``workflow_state``
    block that validates to EXACTLY the transcribed EARTHSCOPE fixture.

    This is the end-to-end re-assertion of the Slice-B pin: the fixture every
    golden merges/grounds/scrubs against is proven identical to the schema the
    shipped pack declares — the declaration and the engine cannot drift.
    """
    text = (_EARTHSCOPE_ROOT / "AGENT.md").read_text(encoding="utf-8")
    meta, _body = _parse_frontmatter(text)

    declaration = meta["workflow_state"]
    assert isinstance(declaration, dict)
    assert WorkflowStateSchema.model_validate(declaration) == EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def test_resolver_returns_typed_schema_for_earthscope_session_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session whose active blueprint is the *installed* EarthScope pack
    resolves the typed schema (not GENERIC) and records no fallback.

    Pins the two blueprint-identity seams to the on-disk EarthScope root and
    lets the resolver parse it for real — the declaration is present, so the
    typed engine is returned and the ``workflow_state_schema_absent`` ledger
    stays empty (the loud generic-fallback path must NOT fire).
    """
    app: Any = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(
        resolution,
        "_runtime_active_agent_blueprint_id",
        lambda app, session_id="": "earthscope-gnss-region",
    )
    monkeypatch.setattr(
        resolution,
        "_runtime_active_agent_blueprint_path",
        lambda app, session_id="": _EARTHSCOPE_ROOT,
    )

    schema = _active_workflow_state_schema(app, "sess-earthscope")

    assert schema is not GENERIC_WORKFLOW_STATE_SCHEMA
    assert schema == EARTHSCOPE_WORKFLOW_STATE_SCHEMA
    # No degradation: the declaration is present, so nothing is recorded.
    assert _workflow_schema_fallbacks(app) == {}
