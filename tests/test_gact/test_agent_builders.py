"""Builder tests for the final_responder answer-stream unification (#736).

Removing the hardcoded ``id == "synthesis"`` string literal means the terminal
child's answer-stream visibility now keys on the SAME declarative
``structured_outputs.final_responder`` flag the settle loop uses. These tests
drive the REAL ``BlueprintExpertModule.forward`` up to its LM call and capture
the ``visible_answer_stream`` contextvar it computes — no behavior mocking.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import builders as _builders
from clio_agent.gact.agents.builders import _build_blueprint_dspy_module
from clio_agent.gact.agents.migration_signals import (
    FINAL_RESPONDER_MIGRATION_REASON,
    check_final_responder_migration,
)
from clio_agent.gact.types import AgentDef


def _fake_spec() -> Any:
    return SimpleNamespace(
        materialize=lambda cred=None: SimpleNamespace(provider="openai", model="m", temperature=0.0)
    )


def _visible_for(monkeypatch: pytest.MonkeyPatch, agent_id: str, **structured: Any) -> bool:
    """Build a real blueprint module and return the ``visible_answer_stream`` value
    the forward computes for the given id + structured_outputs, captured at the LM
    call site (before any LM actually runs)."""

    # Stub the LM factories BEFORE build so the module's closure binds the stubs
    # (they are imported inside _build_blueprint_dspy_module), and no real LM runs.
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _fake_spec(),
        raising=True,
    )

    module = _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(
            id=agent_id,
            source="expert_pack",
            title=agent_id,
            module={"kind": "chain_of_thought"},
            structured_outputs=dict(structured),
        ),
    )

    captured: dict[str, bool] = {}

    def _capture(**kwargs: Any) -> Any:
        captured["visible"] = _ctx.active_visible_answer_stream()
        raise _Captured()

    class _Captured(Exception):
        pass

    module.program = _capture
    try:
        module.forward(question="q", session_id="")
    except Exception:
        # The capturer short-circuits the forward after recording the token; the
        # token is computed BEFORE the LM call, so any downstream error is moot.
        pass
    assert "visible" in captured, "forward never reached the LM call site"
    return captured["visible"]


def test_final_responder_flag_makes_workflow_state_answer_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow_state expert that ALSO declares final_responder streams its answer
    (its answer is the user deliverable). This is the earthscope synthesis case."""

    assert _visible_for(monkeypatch, "synthesis", workflow_state=True, final_responder=True) is True


def test_workflow_state_without_flag_suppresses_answer_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow_state expert WITHOUT the flag keeps its answer non-streaming."""

    assert _visible_for(monkeypatch, "data", workflow_state=True) is False


def test_synthesis_id_without_flag_no_longer_forces_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the ``id == 'synthesis'`` hardcode is GONE: the id alone no longer
    makes a workflow_state expert's answer visible — only the declarative flag does."""

    assert _visible_for(monkeypatch, "synthesis", workflow_state=True) is False


def test_quoted_false_final_responder_does_not_force_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(#736/C) A QUOTED author error (``final_responder: "no"`` / ``"false"``) must NOT
    enable the flag. ``bool("no")`` is ``True`` — the read must route through the shared
    structured-flag truthiness helper, so a workflow_state expert with a quoted-off flag
    keeps its answer SUPPRESSED (visibility falls back to the workflow_state gate)."""

    for quoted_off in ("no", "false", "off", "0", "disabled"):
        assert (
            _visible_for(monkeypatch, "synthesis", workflow_state=True, final_responder=quoted_off)
            is False
        )


def test_quoted_false_workflow_state_keeps_answer_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(#736/C) The workflow_state half of the visibility gate must also honour a quoted
    author error: ``workflow_state: "false"`` is DISABLED, so the expert's answer streams
    (``not bool("false")`` would wrongly SUPPRESS it)."""

    assert _visible_for(monkeypatch, "data", workflow_state="false") is True


# --- #736 config-migration signal (A) ---------------------------------------
# The visibility flip above is CORRECT, but a third-party pack whose expert is
# literally named "synthesis", declares workflow_state, and never adds the new
# final_responder flag silently loses its answer stream. These tests prove the
# construction-path detector surfaces that migration as a loud, always-on signal.


def _fake_app() -> Any:
    """A minimal app carrying a mutable ``.state`` for the per-session ledger."""
    return SimpleNamespace(state=SimpleNamespace())


def _def(agent_id: str, **structured: Any) -> AgentDef:
    return AgentDef(
        id=agent_id,
        source="expert_pack",
        title=agent_id,
        structured_outputs=dict(structured),
    )


def test_migration_signal_fires_for_unmigrated_synthesis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact un-migrated shape (id 'synthesis' + workflow_state, no flag) records
    a structured reason in the per-session ledger AND logs a WARNING naming the fix."""

    app = _fake_app()
    with caplog.at_level(logging.WARNING):
        payload = check_final_responder_migration(
            app, "sess-1", _def("synthesis", workflow_state=True)
        )
    assert payload is not None
    assert payload["reason"] == FINAL_RESPONDER_MIGRATION_REASON
    ledger = app.state.turn_degradations
    assert list(ledger) == ["sess-1"]
    assert ledger["sess-1"][0]["reason"] == FINAL_RESPONDER_MIGRATION_REASON
    assert "final_responder" in ledger["sess-1"][0]["message"]
    assert (
        "add_final_responder_true_to_structured_outputs" in ledger["sess-1"][0]["recovery_actions"]
    )
    assert any("final_responder migration required" in rec.message for rec in caplog.records)


def test_migration_signal_silent_once_flag_added() -> None:
    """Adding ``final_responder: true`` migrates the pack: the signal does NOT fire and
    the per-session ledger stays empty (no attribution to make)."""

    app = _fake_app()
    payload = check_final_responder_migration(
        app, "sess-1", _def("synthesis", workflow_state=True, final_responder=True)
    )
    assert payload is None
    # A migrated pack returns before ever touching the ledger, so the store is
    # never even created on app.state.
    assert getattr(app.state, "turn_degradations", None) is None


def test_migration_signal_ignores_non_synthesis() -> None:
    """An expert with a different id never trips the signal (name is DATA, not a heuristic
    on prose): only the literal pre-#736 'synthesis' shape flipped."""

    app = _fake_app()
    assert check_final_responder_migration(app, "s", _def("data", workflow_state=True)) is None


def test_migration_signal_ignores_synthesis_without_workflow_state() -> None:
    """A 'synthesis' expert that does not run the typed-state engine was VISIBLE before and
    after #736 (visibility = not workflow_state), so nothing migrated -- no signal."""

    app = _fake_app()
    assert check_final_responder_migration(app, "s", _def("synthesis")) is None


def test_migration_signal_fires_on_quoted_off_flag() -> None:
    """A QUOTED author error (``final_responder: "no"``) does NOT enable the flag, so the
    answer still flips hidden -- the migration signal must still fire."""

    app = _fake_app()
    payload = check_final_responder_migration(
        app, "s", _def("synthesis", workflow_state=True, final_responder="no")
    )
    assert payload is not None
    assert payload["reason"] == FINAL_RESPONDER_MIGRATION_REASON


def test_migration_signal_dedups_consecutive_records() -> None:
    """Re-constructing the same un-migrated expert in a session leaves ONE ledger entry
    (consecutive same-message records collapse), so a session cannot grow it unbounded."""

    app = _fake_app()
    unmigrated = _def("synthesis", workflow_state=True)
    check_final_responder_migration(app, "sess-1", unmigrated)
    check_final_responder_migration(app, "sess-1", unmigrated)
    assert len(app.state.turn_degradations["sess-1"]) == 1


def test_migration_signal_appless_returns_payload_without_ledger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """App-less construction still logs the WARNING (immediate author signal) and returns the
    payload, but has nothing to attribute -- no ledger, no crash."""

    with caplog.at_level(logging.WARNING):
        payload = check_final_responder_migration(
            None, "sess-1", _def("synthesis", workflow_state=True)
        )
    assert payload is not None
    assert any("final_responder migration required" in rec.message for rec in caplog.records)


def test_build_path_invokes_migration_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The construction path (BlueprintExpertModule.__init__) actually calls the detector,
    so the signal is wired -- not merely defined."""

    seen: list[str] = []
    monkeypatch.setattr(
        _builders,
        "check_final_responder_migration",
        lambda app, sid, agent_def: seen.append(str(agent_def.id)) or None,
        raising=True,
    )
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object(), raising=True)
    monkeypatch.setattr(
        "clio_agent.config.create_chat_adapter", lambda config: object(), raising=True
    )
    monkeypatch.setattr(
        "clio_agent.gact.agents.builders._dynamic_agent_lm_config",
        lambda base_agent, agent_def: _fake_spec(),
        raising=True,
    )
    _build_blueprint_dspy_module(
        SimpleNamespace(),
        AgentDef(
            id="synthesis",
            source="expert_pack",
            title="synthesis",
            module={"kind": "chain_of_thought"},
            structured_outputs={"workflow_state": True},
        ),
    )
    assert seen == ["synthesis"]
