"""First-batch config migration: real call sites resolve file → env → default.

Each previously-env-only knob now flows through ``clio_agent.conf.resolve`` while
keeping its original environment-variable name (no behavioral break) and gaining
a config-file layer that wins over the env.
"""

from __future__ import annotations

import pytest

from clio_agent import conf


@pytest.fixture(autouse=True)
def _fresh_store():
    """Reset the process-wide config store around each test (conftest points
    XDG_CONFIG_HOME at a per-test tmp dir, so the file layer starts empty)."""
    conf.reload()
    yield
    conf.reload()


def _write_user_config(monkeypatch, tmp_path, body: str) -> None:
    """Write a user config.yaml under the test XDG and refresh the store."""
    import os

    xdg = os.environ["XDG_CONFIG_HOME"]  # set by conftest to a tmp dir
    cfg_dir = tmp_path  # not used; XDG drives discovery
    del cfg_dir
    from pathlib import Path

    target = Path(xdg) / "clio-agent" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    conf.reload()


class TestLmCallTimeout:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("CLIO_MAX_LM_CALL_S", raising=False)
        from clio_agent.runtime.lm_activity import _DEFAULT_MAX_LM_CALL_S, _max_lm_call_seconds

        assert _max_lm_call_seconds() == _DEFAULT_MAX_LM_CALL_S

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "42")
        from clio_agent.runtime.lm_activity import _max_lm_call_seconds

        assert _max_lm_call_seconds() == 42.0

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "not-a-number")
        from clio_agent.runtime.lm_activity import _DEFAULT_MAX_LM_CALL_S, _max_lm_call_seconds

        assert _max_lm_call_seconds() == _DEFAULT_MAX_LM_CALL_S

    def test_nonpositive_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "0")
        from clio_agent.runtime.lm_activity import _DEFAULT_MAX_LM_CALL_S, _max_lm_call_seconds

        assert _max_lm_call_seconds() == _DEFAULT_MAX_LM_CALL_S

    def test_file_wins_over_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "42")
        _write_user_config(monkeypatch, tmp_path, "limits:\n  lm_call_s: 123\n")
        from clio_agent.runtime.lm_activity import _max_lm_call_seconds

        assert _max_lm_call_seconds() == 123.0


class TestAgentMaxSteps:
    def test_default_and_clamp(self, monkeypatch):
        from clio_agent.agent import DEFAULT_AGENT_MAX_STEPS, ClioAgent

        monkeypatch.delenv("CLIO_AGENT_MAX_STEPS", raising=False)
        assert ClioAgent._agent_max_steps() == DEFAULT_AGENT_MAX_STEPS
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "5")
        assert ClioAgent._agent_max_steps() == 5
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "999")  # clamped to 12
        assert ClioAgent._agent_max_steps() == 12
        monkeypatch.setenv("CLIO_AGENT_MAX_STEPS", "garbage")  # falls back
        assert ClioAgent._agent_max_steps() == DEFAULT_AGENT_MAX_STEPS


class TestHookTimeout:
    def test_env_and_default(self, monkeypatch, tmp_path):
        from clio_agent.runtime.hooks import HookRegistry

        monkeypatch.delenv("CLIO_HOOK_TIMEOUT_S", raising=False)
        reg = HookRegistry(hooks_dir=tmp_path / "hooks")
        assert reg._timeout_s == 5.0
        monkeypatch.setenv("CLIO_HOOK_TIMEOUT_S", "2.5")
        reg2 = HookRegistry(hooks_dir=tmp_path / "hooks")
        assert reg2._timeout_s == 2.5

    def test_explicit_arg_beats_config(self, monkeypatch, tmp_path):
        from clio_agent.runtime.hooks import HookRegistry

        monkeypatch.setenv("CLIO_HOOK_TIMEOUT_S", "2.5")
        reg = HookRegistry(hooks_dir=tmp_path / "hooks", timeout_s=9.0)
        assert reg._timeout_s == 9.0


class TestGactTurnTimeout:
    def test_env_and_file(self, monkeypatch, tmp_path):
        from clio_agent.gact import app

        monkeypatch.delenv("CLIO_GACT_TURN_TIMEOUT_S", raising=False)
        assert app._gact_turn_timeout_s() == 900.0
        monkeypatch.setenv("CLIO_GACT_TURN_TIMEOUT_S", "120")
        assert app._gact_turn_timeout_s() == 120.0
        # file overrides env
        _write_user_config(monkeypatch, tmp_path, "limits:\n  turn_timeout_s: 77\n")
        assert app._gact_turn_timeout_s() == 77.0
