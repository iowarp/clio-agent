"""Keyed claude_code SDK session pool + per-LM transport (#818, step 10).

These prove the two guarantees the per-expert-provider design requires for
``claude_code`` experts:

1. **Keyed session pool** — two concurrent experts wanting *different*
   ``claude_code`` models each hold their own persistent SDK session, so they run
   concurrently instead of thrashing one shared single-connection singleton.
2. **Per-LM transport** — transport is read purely from the per-LM
   ``optional_params`` carried on the resolved ``LMProviderConfig``; the
   process-global ``CLIO_CLAUDE_CODE_TRANSPORT`` env var is *not* consulted, so
   concurrent experts never share an ambient transport.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_litellm import (
    ClaudeCodeLLM,
    _SdkSessionPool,
)


@pytest.fixture(autouse=True)
def reset_provider() -> None:
    """Each test starts and ends with a clean LiteLLM provider map."""
    claude_code_litellm._reset_for_tests()
    yield
    claude_code_litellm._reset_for_tests()


def test_pool_reuses_one_session_per_key() -> None:
    """Same ``(model, cwd)`` reuses one session; distinct keys get distinct ones."""
    pool = _SdkSessionPool()

    sonnet_a = pool._session_for("sonnet", "/w")
    sonnet_b = pool._session_for("sonnet", "/w")
    haiku = pool._session_for("haiku", "/w")
    sonnet_other_cwd = pool._session_for("sonnet", "/other")

    # Same key -> same session (persistent connection reuse).
    assert sonnet_a is sonnet_b
    # Different model -> different session (no shared-connection thrash).
    assert sonnet_a is not haiku
    # Different cwd -> different session (cwd is part of the key).
    assert sonnet_a is not sonnet_other_cwd


def test_concurrent_distinct_models_hold_own_sessions(monkeypatch) -> None:
    """Two concurrent distinct-model experts each hold their own SDK session.

    Each faked completion blocks on a 2-party barrier before returning. If the two
    distinct-model calls were serialized onto one shared connection, only one would
    ever be in-flight and the barrier would time out (BrokenBarrierError). The pool
    routes them to *separate* sessions, so both reach the barrier at once and each
    is handled by a distinct ``_SdkSession`` instance — no thrash.
    """
    pool = _SdkSessionPool()
    barrier = threading.Barrier(2, timeout=5.0)
    handled_by: dict[str, int] = {}
    lock = threading.Lock()

    def fake_complete(self, *, prompt: str, model: str, timeout, cwd, thinking=None):
        # Both concurrent calls must be here simultaneously; a shared serialized
        # session would deadlock this barrier.
        barrier.wait()
        with lock:
            handled_by[model] = id(self)
        return f"ok-{model}", {}

    monkeypatch.setattr(claude_code_litellm._SdkSession, "complete", fake_complete, raising=True)

    results: dict[str, tuple] = {}

    def run(model: str) -> None:
        results[model] = pool.complete(prompt="p", model=model, timeout=1.0, cwd="/w")

    threads = [
        threading.Thread(target=run, args=("sonnet",)),
        threading.Thread(target=run, args=("haiku",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=6.0)

    assert all(not thread.is_alive() for thread in threads), "barrier deadlocked"
    assert results["sonnet"] == ("ok-sonnet", {})
    assert results["haiku"] == ("ok-haiku", {})
    # Each model was serviced by its own session instance.
    assert handled_by["sonnet"] != handled_by["haiku"]
    # And the sessions persist in the pool keyed per model.
    assert pool._session_for("sonnet", "/w") is not pool._session_for("haiku", "/w")


def test_run_sdk_delegates_to_the_module_pool(monkeypatch) -> None:
    """``_run_sdk`` routes through the process-wide keyed session pool."""
    seen: dict = {}

    def fake_complete(*, prompt, model, timeout, cwd, thinking=None):
        seen.update(prompt=prompt, model=model, timeout=timeout, cwd=cwd)
        return "pooled", {"input_tokens": 1, "output_tokens": 2}

    monkeypatch.setattr(claude_code_litellm._SDK_SESSION_POOL, "complete", fake_complete)

    text, usage = claude_code_litellm._run_sdk(
        prompt="hello", model="sonnet", timeout=12.0, cwd="/w"
    )

    assert text == "pooled"
    assert usage == {"input_tokens": 1, "output_tokens": 2}
    assert seen == {"prompt": "hello", "model": "sonnet", "timeout": 12.0, "cwd": "/w"}


def test_transport_read_from_config_only_ignores_env(monkeypatch) -> None:
    """Transport comes only from optional_params; the env var is not consulted.

    With ``CLIO_CLAUDE_CODE_TRANSPORT=exec`` set but no override in
    ``optional_params``, the DEFAULT_TRANSPORT (``sdk``) applies — proving the
    process-global env never leaks into the per-LM path.
    """
    monkeypatch.setenv("CLIO_CLAUDE_CODE_TRANSPORT", "exec")

    def fake_sdk(*, prompt, model, timeout, cwd, thinking=None):
        return "sdk path", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_sdk)

    resp = ClaudeCodeLLM().completion(
        model="claude_code/cc-sonnet",
        messages=[{"role": "user", "content": "hi"}],
        api_base="",
        custom_prompt_dict={},
        model_response=MagicMock(),
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},  # no transport override -> DEFAULT_TRANSPORT (sdk)
    )

    assert resp.choices[0].message.content == "sdk path"


def test_optional_params_transport_overrides_env(monkeypatch) -> None:
    """The per-LM optional_params transport is authoritative over the (ignored) env.

    v0.8.0: with the "exec" transport deleted, an explicit removed transport in
    optional_params raises the typed error even when the env says "sdk" — the
    env is never consulted on the per-LM path (#818).
    """
    monkeypatch.setenv("CLIO_CLAUDE_CODE_TRANSPORT", "sdk")

    def fake_sdk(**_kwargs):
        raise AssertionError("sdk transport must not be selected from env")

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_sdk)

    from clio_agent.providers.claude_code_litellm import ClaudeCodeExecError

    with pytest.raises(ClaudeCodeExecError, match="removed in the v0.8.0 cleanup"):
        ClaudeCodeLLM().completion(
            model="claude_code/cc-sonnet",
            messages=[{"role": "user", "content": "hi"}],
            api_base="",
            custom_prompt_dict={},
            model_response=MagicMock(),
            print_verbose=None,
            encoding=None,
            api_key=None,
            logging_obj=None,
            optional_params={"claude_code_transport": "exec"},
        )
