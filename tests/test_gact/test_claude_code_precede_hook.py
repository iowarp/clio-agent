"""B2 (S2 Claude SDK tuning): the session-open Claude Code precede-connect hook.

:func:`~clio_agent.gact.agents.claude_code_precede.precede_connect_claude_code_session`
is the thin, owner-module function every claude_code-backed dynamic-agent
module build calls with its REAL resolved config -- see that module's
docstring for the full contract. This file pins:

* the pure function itself: no-ops for a non-claude_code provider, an empty
  session id, or a missing signature; resolves the real model/thinking/
  system_prompt and hands them to the pool for a claude_code config; a
  resolution failure never raises.
* the three ``gact.agents.runners`` call sites actually wire it in, passing
  each module's own resolved config/signature.

Pool-side behaviour (cap, eviction, single-connect reuse, failure isolation)
is pinned in ``tests/test_providers/test_claude_code_precede_connect.py`` --
this file only pins the gact-side hook that feeds it the real config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dspy
import pytest

from clio_agent.gact.agents import claude_code_precede as hook
from clio_agent.gact.agents import runners
from clio_agent.providers import claude_code_sessions as ccs

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


class _FakeSignature(dspy.Signature):
    """Answer the question."""

    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


def _fake_config(**overrides: Any) -> Any:
    from clio_agent.config import LMProviderConfig

    fields = {"provider": "claude_code", "model": "claude-haiku-20241022", **overrides}
    return LMProviderConfig(**fields)


def _capture_precede_connect(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(ccs._STREAM_CLIENT_POOL, "precede_connect", _fake)
    return calls


# --------------------------------------------------------------------------- #
# The pure function's no-op guards.
# --------------------------------------------------------------------------- #
def test_noop_for_a_non_claude_code_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_precede_connect(monkeypatch)
    from clio_agent.config import LMProviderConfig

    config = LMProviderConfig(provider="anthropic", model="claude-haiku-20241022")

    hook.precede_connect_claude_code_session(config, _FakeSignature, session_id="sess-1")

    assert calls == []


def test_noop_for_an_empty_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_precede_connect(monkeypatch)

    hook.precede_connect_claude_code_session(_fake_config(), _FakeSignature, session_id="")

    assert calls == []


def test_noop_when_signature_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_precede_connect(monkeypatch)

    hook.precede_connect_claude_code_session(_fake_config(), None, session_id="sess-1")

    assert calls == []


def test_noop_when_config_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_precede_connect(monkeypatch)

    hook.precede_connect_claude_code_session(None, _FakeSignature, session_id="sess-1")

    assert calls == []


# --------------------------------------------------------------------------- #
# The real config reaches the pool.
# --------------------------------------------------------------------------- #
def test_resolves_model_and_system_prompt_and_calls_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SABOTAGE: pass ``signature=None`` down to ``format_system_message`` (or
    skip the pool call entirely) -> ``calls`` stays empty or ``system_prompt``
    goes ``None`` -> the assertions below go red.
    """
    calls = _capture_precede_connect(monkeypatch)
    config = _fake_config(model="claude-sonnet-4", thinking_level="off")

    hook.precede_connect_claude_code_session(config, _FakeSignature, session_id="sess-real")

    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == "sess-real"
    assert call["model"] == "claude-sonnet-4"
    assert call["system_prompt"]  # a real rendered system message, not None/empty
    assert "answer" in call["system_prompt"] or "question" in call["system_prompt"]


def test_thinking_off_resolves_to_the_disabled_sdk_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_precede_connect(monkeypatch)
    config = _fake_config(thinking_level="off")

    hook.precede_connect_claude_code_session(config, _FakeSignature, session_id="sess-thinking")

    assert calls[0]["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- #
# A resolution failure never reaches the caller.
# --------------------------------------------------------------------------- #
def test_a_resolution_failure_is_swallowed_and_never_calls_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_precede_connect(monkeypatch)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("thinking resolution exploded")

    import clio_agent.providers.thinking as thinking_mod

    monkeypatch.setattr(thinking_mod, "resolve_thinking", _boom)

    # Must not raise.
    hook.precede_connect_claude_code_session(_fake_config(), _FakeSignature, session_id="sess-x")

    assert calls == []


# --------------------------------------------------------------------------- #
# The runners.py call sites actually wire the hook in.
# --------------------------------------------------------------------------- #
class _FakeModule:
    def __init__(self, config: Any, signature: Any) -> None:
        self.config = config
        self.signature = signature
        self.forward_calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> str:
        self.forward_calls.append(kwargs)
        return "blueprint-result"

    def forward(self, **kwargs: Any) -> str:
        self.forward_calls.append(kwargs)
        return "forward-result"


def _capture_hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(config: Any, signature: Any, *, session_id: str) -> None:
        calls.append({"config": config, "signature": signature, "session_id": session_id})

    monkeypatch.setattr(runners, "precede_connect_claude_code_session", _fake)
    return calls


def test_run_blueprint_dspy_agent_wires_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_hook_calls(monkeypatch)
    config = _fake_config()
    module = _FakeModule(config, _FakeSignature)
    monkeypatch.setattr(
        runners, "_app_builder", lambda name: (lambda base_agent, agent_def: module)
    )

    result = runners._run_blueprint_dspy_agent(
        base_agent=object(),
        agent_def=cast("AgentDef", object()),
        question="hi",
        session_id="sess-bp",
    )

    assert result == "blueprint-result"
    assert len(calls) == 1
    assert calls[0]["config"] is config
    assert calls[0]["signature"] is _FakeSignature
    assert calls[0]["session_id"] == "sess-bp"


def test_run_prompt_user_agent_wires_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_hook_calls(monkeypatch)
    config = _fake_config()
    module = _FakeModule(config, _FakeSignature)
    monkeypatch.setattr(
        runners, "_app_builder", lambda name: (lambda base_agent, agent_def: module)
    )

    result = runners._run_prompt_user_agent(
        base_agent=object(),
        agent_def=cast("AgentDef", object()),
        question="hi",
        session_id="sess-prompt",
    )

    assert result == "forward-result"
    assert len(calls) == 1
    assert calls[0]["config"] is config
    assert calls[0]["session_id"] == "sess-prompt"


def test_run_tool_user_agent_wires_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_hook_calls(monkeypatch)
    config = _fake_config()
    module = _FakeModule(config, _FakeSignature)
    monkeypatch.setattr(
        runners, "_app_builder", lambda name: (lambda base_agent, agent_def: module)
    )

    result = runners._run_tool_user_agent(
        base_agent=object(),
        agent_def=cast("AgentDef", object()),
        question="hi",
        session_id="sess-tool",
    )

    assert result == "forward-result"
    assert len(calls) == 1
    assert calls[0]["config"] is config
    assert calls[0]["session_id"] == "sess-tool"


def test_a_pool_exception_inside_the_hook_still_never_breaks_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the REAL hook (not a stand-in) with a pool whose ``precede_connect``
    raises -- the hook's own internal try/except (pinned directly above) is
    what makes the whole feature safe end to end through a real runner call.
    """

    def _boom_pool_call(**kwargs: Any) -> None:
        raise RuntimeError("pool exploded")

    monkeypatch.setattr(ccs._STREAM_CLIENT_POOL, "precede_connect", _boom_pool_call)
    module = _FakeModule(_fake_config(), _FakeSignature)
    monkeypatch.setattr(
        runners, "_app_builder", lambda name: (lambda base_agent, agent_def: module)
    )

    result = runners._run_prompt_user_agent(
        base_agent=object(),
        agent_def=cast("AgentDef", object()),
        question="hi",
        session_id="sess-y",
    )

    assert result == "forward-result"  # the turn completed despite the pool blowing up
