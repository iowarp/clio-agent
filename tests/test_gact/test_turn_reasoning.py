"""Per-message reasoning effort reaches the turn's LM (I).

``behavior.reasoning_effort`` was accepted and stored but never read: only the
global ``thinking_level`` reached the LM. These tests drive the REAL chain a turn
uses -- the turn-local agent overlay, ``_dynamic_agent_lm_config`` (spec ->
resolver), ``materialize`` and ``create_lm`` -- and assert the provider-correct
kwargs land on the constructed ``dspy.LM``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact.agents.builders import _dynamic_agent_lm_config
from clio_agent.gact.message_contract import MessageBehavior, PostMessageRequest
from clio_agent.gact.turn_reasoning import apply_turn_reasoning, message_reasoning_effort
from clio_agent.gact.types import AgentDef, Message
from clio_agent.lm.factory import create_lm
from clio_agent.providers import resolver as resolver_mod
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)

_NOW = "2026-09-25T00:00:00+00:00"


def _seed_thinking(
    *,
    provider_id: str,
    api_base: str,
    model_id: str,
    dialect: str,
    thinking_controls: frozenset[str],
    thinking_spec: ThinkingSpec,
) -> None:
    """Seed the real per-model ThinkingSpec a live handshake would have
    linked, so the request builder's fail-closed thinking_wire has known
    evidence to work from (see dialect_wire.thinking_wire's docstring)."""

    model_key = f"test:{provider_id}:{model_id}"
    invalidation.record_endpoint_capabilities(
        EndpointCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            dialect=dialect,
            thinking_controls=Fact(thinking_controls, "dialect", _NOW),
        )
    )
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key=model_key,
            thinking=Fact(thinking_spec, "server_report", _NOW),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id=provider_id,
            api_base=api_base,
            model_id=model_id,
            model_key=Fact(model_key, "server_report", _NOW),
        )
    )


def _message(effort: str | None) -> Message:
    behavior: dict[str, Any] = {"execution_mode": "execute", "confirmation_policy": "ask"}
    if effort is not None:
        behavior["reasoning_effort"] = effort
    return Message(
        id="msg_1",
        session_id="sess_1",
        role="user",
        created_at="2026-09-23T00:00:00+00:00",
        updated_at="2026-09-23T00:00:00+00:00",
        parts=[],
        metadata={"behavior": behavior},
    )


@pytest.fixture(autouse=True)
def _reset_capability_state():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: None)

    def _handshake(ctx: Any, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id=ctx.provider_id,
            provider_kind=ctx.provider_kind,
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            api_base=ctx.api_base,
            models=(DiscoveredModel(id=ctx.target_model or "m"),),
        )

    monkeypatch.setattr(resolver_mod, "run_handshake_sync", _handshake)


def _lm_kwargs(base: LMProviderConfig, effort: str | None) -> dict[str, Any]:
    agent_def = apply_turn_reasoning(_message(effort), AgentDef(id="main", title="Main"))
    resolved = _dynamic_agent_lm_config(SimpleNamespace(_provider_config=base), agent_def)
    cfg = resolved.materialize(SimpleNamespace(resolve=lambda *_args: "test-credential"))
    return dict(create_lm(cfg).kwargs)


def test_codex_message_effort_overrides_the_global_level() -> None:
    _seed_thinking(
        provider_id="codex",
        api_base="codex://sdk",
        model_id="gpt-5.5",
        dialect="codex",
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=("low", "high", "xhigh"),
            effort_by_level={"low": "low", "high": "high", "xhigh": "xhigh"},
        ),
    )
    base = LMProviderConfig(provider="codex", model="gpt-5.5", thinking_level="low")
    assert _lm_kwargs(base, "high")["codex_reasoning_effort"] == "high"
    assert _lm_kwargs(base, "xhigh")["codex_reasoning_effort"] == "xhigh"
    # No per-message level: the configured level still governs.
    assert _lm_kwargs(base, None)["codex_reasoning_effort"] == "low"


def test_openai_kind_message_effort_sets_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed_thinking(
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        model_id="gpt-5",
        dialect="openai",
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(mechanism="effort_levels", levels=("high",), effort_by_level={}),
    )
    base = LMProviderConfig(provider="openai", model="gpt-5", api_key="sk-test")
    assert _lm_kwargs(base, "high")["reasoning_effort"] == "high"
    # No message effort and no configured level: thinking is off, and this
    # model reports no "off" level of its own -- nothing sent (fail closed).
    assert "reasoning_effort" not in _lm_kwargs(base, None)


def test_argonne_message_effort_sets_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argonne runs the vLLM dialect (brief 4.1): gpt-oss's own effort spelling
    IS its chat-template kwarg name, sent under extra_body (never top-level)."""
    monkeypatch.setenv("CLIO_ARGONNE_TOKEN", "alcf-test")
    api_base = "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"
    _seed_thinking(
        provider_id="argonne_metis",
        api_base=api_base,
        model_id="openai/gpt-oss-120b",
        dialect="vllm",
        thinking_controls=frozenset({"chat_template_kwargs"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=("low", "medium", "high"),
            template_kwarg="reasoning_effort",
        ),
    )
    base = LMProviderConfig(
        provider="argonne",
        provider_id="argonne_metis",
        model="openai/gpt-oss-120b",
        api_key="alcf-test",
    )
    kwargs = _lm_kwargs(base, "high")
    assert kwargs["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}


def test_anthropic_message_effort_sets_a_thinking_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _seed_thinking(
        provider_id="anthropic",
        api_base="https://api.anthropic.com/v1",
        model_id="claude-sonnet-4-5",
        dialect="anthropic",
        thinking_controls=frozenset({"anthropic_thinking"}),
        thinking_spec=ThinkingSpec(mechanism="budget_tokens"),
    )
    base = LMProviderConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="sk-ant")
    assert _lm_kwargs(base, "high")["thinking"] == {"type": "enabled", "budget_tokens": 24576}
    assert "thinking" not in _lm_kwargs(base, "off")


def test_claude_code_message_effort_sets_the_sdk_thinking_config() -> None:
    _seed_thinking(
        provider_id="claude_code",
        api_base="claude-code://sdk",
        model_id="opus",
        dialect="claude_code",
        thinking_controls=frozenset({"claude_code_thinking"}),
        thinking_spec=ThinkingSpec(mechanism="budget_tokens"),
    )
    base = LMProviderConfig(provider="claude_code", model="opus")
    assert _lm_kwargs(base, "medium")["claude_code_thinking"] == {
        "type": "enabled",
        "budget_tokens": 8192,
        "display": "summarized",
    }


def test_overlay_is_local_and_leaves_the_stored_agent_untouched() -> None:
    stored = AgentDef(id="main", title="Main", parameters={"temperature": 0.2})
    turn_local = apply_turn_reasoning(_message("high"), stored)
    assert turn_local.parameters == {"temperature": 0.2, "thinking_level": "high"}
    assert turn_local.metadata["turn_reasoning_source"] == "per_message"
    assert stored.parameters == {"temperature": 0.2}
    assert apply_turn_reasoning(_message(None), stored) is stored


def test_unset_effort_is_absent_not_a_fabricated_default() -> None:
    assert MessageBehavior().reasoning_effort is None
    assert "reasoning_effort" not in PostMessageRequest().behavior.model_dump(exclude_none=True)
    assert message_reasoning_effort(_message(None)) == ""


def test_claude_code_message_effort_sends_the_sdk_effort() -> None:
    """A claude_code model reporting CLI effort levels gets the real SDK effort."""
    levels = ("low", "medium", "high", "xhigh", "max")
    _seed_thinking(
        provider_id="claude_code",
        api_base="claude-code://sdk",
        model_id="claude-fable-5-1",
        dialect="claude_code",
        thinking_controls=frozenset({"effort", "claude_code_thinking"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels",
            levels=levels,
            effort_by_level={level: level for level in levels},
        ),
    )
    base = LMProviderConfig(provider="claude_code", model="claude-fable-5-1")
    assert _lm_kwargs(base, "max")["claude_code_thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "effort": "max",
    }
    assert _lm_kwargs(base, "off")["claude_code_thinking"] == {"type": "disabled"}


def test_anthropic_adaptive_model_message_effort_sends_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _seed_thinking(
        provider_id="anthropic",
        api_base="https://api.anthropic.com/v1",
        model_id="claude-opus-4-7",
        dialect="anthropic",
        thinking_controls=frozenset({"reasoning_effort", "anthropic_thinking"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels", levels=("low", "medium", "high", "max"), effort_by_level={}
        ),
    )
    base = LMProviderConfig(provider="anthropic", model="claude-opus-4-7", api_key="sk-ant")
    assert _lm_kwargs(base, "max")["reasoning_effort"] == "max"


def test_codex_message_minimal_effort_is_sent() -> None:
    _seed_thinking(
        provider_id="codex",
        api_base="codex://sdk",
        model_id="gpt-5.5",
        dialect="codex",
        thinking_controls=frozenset({"reasoning_effort"}),
        thinking_spec=ThinkingSpec(
            mechanism="effort_levels", levels=("minimal",), effort_by_level={"minimal": "minimal"}
        ),
    )
    base = LMProviderConfig(provider="codex", model="gpt-5.5")
    assert _lm_kwargs(base, "minimal")["codex_reasoning_effort"] == "minimal"


def _record(
    app: Any, turn_id: str, provider_id: str, model_id: str, level: str, source: str
) -> None:
    from clio_agent.gact.turn_reasoning import record_turn_reasoning

    record_turn_reasoning(
        app,
        "sess_parent",
        turn_id,
        {
            "model": {"provider_id": provider_id, "model_id": model_id},
            "reasoning": {"requested_level": level, "source": source},
        },
    )


def _parent_app(provider_id: str, model_id: str, level: str = "max", source: str = "per_message"):
    tasks = {"task_1": SimpleNamespace(parent_turn_id="turn_parent_1")}
    app = SimpleNamespace(
        state=SimpleNamespace(
            lm_config={}, agent=None, agent_task_registry=SimpleNamespace(get=tasks.get)
        )
    )
    _record(app, "turn_parent_1", provider_id, model_id, level, source)
    return app


def _child_message() -> Message:
    message = _message(None)
    message.metadata["agent_task_id"] = "task_1"
    return message


def test_child_on_the_same_model_inherits_the_parent_message_level() -> None:
    app = _parent_app("claude_code", "claude-fable-5-1")
    child = AgentDef(
        id="child", title="Child", default_provider="claude_code", default_model="claude-fable-5-1"
    )

    resolved = apply_turn_reasoning(_child_message(), child, app=app)  # type: ignore[arg-type]

    assert resolved.parameters["thinking_level"] == "max"
    assert resolved.metadata["turn_reasoning_source"] == "parent_message"
    assert resolved.metadata["turn_reasoning_inheritance"] == {"inherited": True}


def test_a_later_parent_turn_cannot_change_a_spawned_childs_level() -> None:
    """Inheritance is keyed by the parent TURN captured at spawn, not the session."""
    app = _parent_app("claude_code", "claude-fable-5-1", level="max")
    # The parent moves on: its next turn runs on a different level.
    _record(app, "turn_parent_2", "claude_code", "claude-fable-5-1", "low", "per_message")
    child = AgentDef(
        id="child", title="Child", default_provider="claude_code", default_model="claude-fable-5-1"
    )

    resolved = apply_turn_reasoning(_child_message(), child, app=app)  # type: ignore[arg-type]

    assert resolved.parameters["thinking_level"] == "max"


def test_child_on_a_different_model_records_why_it_did_not_inherit() -> None:
    app = _parent_app("claude_code", "claude-fable-5-1")
    child = AgentDef(
        id="child", title="Child", default_provider="argonne_metis", default_model="gpt-oss-120b"
    )

    resolved = apply_turn_reasoning(_child_message(), child, app=app)  # type: ignore[arg-type]

    assert "thinking_level" not in resolved.parameters
    assert resolved.metadata["turn_reasoning_inheritance"] == {
        "inherited": False,
        "reason": "different_model",
        "parent_level": "max",
    }


def test_child_does_not_inherit_a_global_level() -> None:
    app = _parent_app("codex", "gpt-5.5", level="high", source="global")
    child = AgentDef(id="child", title="Child", default_provider="codex", default_model="gpt-5.5")

    resolved = apply_turn_reasoning(_child_message(), child, app=app)  # type: ignore[arg-type]

    assert resolved is child


def test_deleting_a_session_prunes_its_turn_records() -> None:
    from clio_agent.gact.session_descendants import purge_session_tasks

    app = _parent_app("codex", "gpt-5.5")
    app.state.agent_task_registry = None
    purge_session_tasks(app, "sess_parent")  # type: ignore[arg-type]
    assert app.state.turn_reasoning_by_turn == {}


def _lm_app(**lm_config: Any) -> Any:
    return SimpleNamespace(state=SimpleNamespace(lm_config=lm_config, agent=None))


def test_put_lm_keeps_only_a_user_level_and_only_on_the_same_model() -> None:
    """A shipped default is not a choice, and no level follows the person to another model."""
    from clio_agent.gact.lm_provider_types import LMProviderRequest
    from clio_agent.gact.providers.config import requested_thinking_level, thinking_level_record

    def put(**fields: Any) -> LMProviderRequest:
        return LMProviderRequest(provider="claude_code", api_base="", **fields)

    # sonnet's shipped "low" is recorded with no source...
    sonnet_default = _lm_app(provider="claude_code", model="sonnet", thinking_level="low")
    # ...so moving to opus with the level omitted does NOT carry it over.
    assert requested_thinking_level(sonnet_default, put(model="opus")) is None
    assert requested_thinking_level(sonnet_default, put(model="sonnet")) is None

    chosen = _lm_app(
        provider="claude_code",
        model="opus",
        thinking_level="max",
        user_thinking_level="max",
        thinking_level_source="user",
    )
    assert requested_thinking_level(chosen, put(model="opus")) == "max"  # same model: kept
    assert requested_thinking_level(chosen, put(model="sonnet")) is None  # other model: unset
    other = LMProviderRequest(provider="lm_studio", api_base="", model="opus")
    assert requested_thinking_level(chosen, other) is None  # other provider: unset
    assert requested_thinking_level(chosen, put(model="opus", thinking_level=None)) is None
    assert requested_thinking_level(chosen, put(model="opus", thinking_level="low")) == "low"
    assert thinking_level_record(chosen, put(model="opus", thinking_level="low")) == {
        "user_thinking_level": "low",
        "thinking_level_source": "user",
    }
    assert thinking_level_record(sonnet_default, put(model="opus")) == {
        "user_thinking_level": None,
        "thinking_level_source": None,
    }


def test_sonnet_to_opus_apply_runs_opus_on_its_own_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on the real config: sonnet ships low; opus must not inherit it.

    The shipped default is DATA (catalogs/claude-code-models.json's own
    ``shipped_default_effort`` field, resolved through the maintained
    catalog's disk cache) -- patched directly here so this test stays
    hermetic and focused on LMProviderConfig/requested_thinking_level's own
    behavior, which is what it actually pins.
    """
    from clio_agent.gact.lm_provider_types import LMProviderRequest
    from clio_agent.gact.providers.config import requested_thinking_level

    monkeypatch.setattr(
        "clio_agent.providers.capabilities.dialects.claude_code.shipped_default_effort_for_model",
        lambda model_id: "low" if model_id == "sonnet" else "",
    )
    sonnet = LMProviderConfig(provider="claude_code", model="sonnet")
    assert sonnet.thinking_level == "low"  # shipped default
    app = _lm_app(provider="claude_code", model="sonnet", thinking_level=sonnet.thinking_level)
    req = LMProviderRequest(provider="claude_code", api_base="", model="opus")
    opus = LMProviderConfig(
        provider="claude_code", model="opus", thinking_level=requested_thinking_level(app, req)
    )
    assert opus.thinking_level is None
