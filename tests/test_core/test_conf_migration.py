"""First-batch config migration: real call sites resolve file → env → default.

Each previously-env-only knob now flows through ``clio_agent.conf.resolve`` while
keeping its original environment-variable name (no behavioral break) and gaining
a config-file layer that wins over the env.
"""

from __future__ import annotations

from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Slice 1 — ad-hoc CLIO_* reads converged on conf.resolve + one as_bool truthy
# --------------------------------------------------------------------------- #


class TestThinkingDisabled:
    """``lm.disable_thinking`` / ``CLIO_LM_DISABLE_THINKING`` — the single shared
    truthy knob read by both ``_provider_lm_kwargs`` and the builders prompt."""

    def test_default(self, monkeypatch):
        from clio_agent.config import _thinking_disabled

        monkeypatch.delenv("CLIO_LM_DISABLE_THINKING", raising=False)
        assert _thinking_disabled() is False

    def test_env(self, monkeypatch):
        from clio_agent.config import _thinking_disabled

        monkeypatch.setenv("CLIO_LM_DISABLE_THINKING", "yes")
        assert _thinking_disabled() is True

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.config import _thinking_disabled

        monkeypatch.setenv("CLIO_LM_DISABLE_THINKING", "1")
        _write_user_config(monkeypatch, tmp_path, "lm:\n  disable_thinking: false\n")
        assert _thinking_disabled() is False


class TestReasoningModelCapability:
    """``lm.reasoning_model`` / ``CLIO_LM_REASONING_MODEL`` — tri-state: an
    explicit file/env value forces the flag; absence falls through to detection."""

    @staticmethod
    def _cfg(is_reasoning=False):
        from types import SimpleNamespace

        return SimpleNamespace(provider="openai", model="gpt-4o", is_reasoning=is_reasoning)

    def test_default_falls_through_to_detection(self, monkeypatch):
        from clio_agent.config import _reasoning_model_capability

        monkeypatch.delenv("CLIO_LM_REASONING_MODEL", raising=False)
        assert _reasoning_model_capability(self._cfg(is_reasoning=False)) is False
        assert _reasoning_model_capability(self._cfg(is_reasoning=True)) is True

    def test_env_forces_true(self, monkeypatch):
        from clio_agent.config import _reasoning_model_capability

        monkeypatch.setenv("CLIO_LM_REASONING_MODEL", "1")
        assert _reasoning_model_capability(self._cfg(is_reasoning=False)) is True

    def test_file_forces_false_over_detection(self, monkeypatch, tmp_path):
        from clio_agent.config import _reasoning_model_capability

        monkeypatch.delenv("CLIO_LM_REASONING_MODEL", raising=False)
        _write_user_config(monkeypatch, tmp_path, "lm:\n  reasoning_model: false\n")
        # capability detection would say True, but the explicit file value wins.
        assert _reasoning_model_capability(self._cfg(is_reasoning=True)) is False


class TestParseRetryAttempts:
    """``limits.lm_parse_retry_attempts`` / ``CLIO_LM_PARSE_RETRY_ATTEMPTS``."""

    @staticmethod
    def _cfg(is_reasoning=False):
        from types import SimpleNamespace

        return SimpleNamespace(provider="openai", model="gpt-4o", is_reasoning=is_reasoning)

    def test_default(self, monkeypatch):
        from clio_agent.config import _parse_retry_attempts

        monkeypatch.delenv("CLIO_LM_PARSE_RETRY_ATTEMPTS", raising=False)
        assert _parse_retry_attempts(self._cfg(is_reasoning=False)) == 0
        assert _parse_retry_attempts(self._cfg(is_reasoning=True)) == 2

    def test_env(self, monkeypatch):
        from clio_agent.config import _parse_retry_attempts

        monkeypatch.setenv("CLIO_LM_PARSE_RETRY_ATTEMPTS", "5")
        assert _parse_retry_attempts(self._cfg()) == 5

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.config import _parse_retry_attempts

        monkeypatch.setenv("CLIO_LM_PARSE_RETRY_ATTEMPTS", "5")
        _write_user_config(monkeypatch, tmp_path, "limits:\n  lm_parse_retry_attempts: 9\n")
        assert _parse_retry_attempts(self._cfg()) == 9


# NOTE (#948 S4b): ``TestLegacyNativeExpertsEnabled`` was deleted alongside the
# retirement of the ``agents.enable_legacy_native_experts`` /
# ``CLIO_AGENT_ENABLE_LEGACY_NATIVE_EXPERTS`` knob. The legacy native-expert
# runtime it gated (the Tier-1 ``ClioAgent.forward`` planner) no longer exists, so
# there is no config surface left to migrate.


class TestStreamAuditEnabled:
    """``debug.stream_audit_log`` / ``CLIO_STREAM_AUDIT_LOG`` (a path -> truthy)."""

    def test_default(self, monkeypatch):
        from clio_agent.runtime.stream_audit import stream_audit_enabled

        monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
        assert stream_audit_enabled() is False

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.runtime.stream_audit import stream_audit_enabled

        monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
        assert stream_audit_enabled() is True

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.runtime.stream_audit import stream_audit_enabled

        monkeypatch.delenv("CLIO_STREAM_AUDIT_LOG", raising=False)
        _write_user_config(
            monkeypatch,
            tmp_path,
            f"debug:\n  stream_audit_log: {(tmp_path / 'a.jsonl').as_posix()}\n",
        )
        assert stream_audit_enabled() is True


class TestMcpReconnectTimeout:
    """``limits.mcp_reconnect_timeout_s`` / env (float, non-positive -> 15s)."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.routes.mcp import _mcp_reconnect_timeout_s

        monkeypatch.delenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", raising=False)
        assert _mcp_reconnect_timeout_s() == 15.0

    def test_env(self, monkeypatch):
        from clio_agent.gact.routes.mcp import _mcp_reconnect_timeout_s

        monkeypatch.setenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", "30")
        assert _mcp_reconnect_timeout_s() == 30.0

    def test_nonpositive_and_garbage_fall_back(self, monkeypatch):
        from clio_agent.gact.routes.mcp import _mcp_reconnect_timeout_s

        monkeypatch.setenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", "0")
        assert _mcp_reconnect_timeout_s() == 15.0
        monkeypatch.setenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", "not-a-number")
        assert _mcp_reconnect_timeout_s() == 15.0

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.routes.mcp import _mcp_reconnect_timeout_s

        monkeypatch.setenv("CLIO_GACT_MCP_RECONNECT_TIMEOUT_S", "30")
        _write_user_config(monkeypatch, tmp_path, "limits:\n  mcp_reconnect_timeout_s: 42\n")
        assert _mcp_reconnect_timeout_s() == 42.0


class TestTransientProviderRetryDelays:
    """``limits.transient_provider_retry_delays`` / env, ``as_csv``; the
    "false/off/none/disabled disables" contract is preserved."""

    def test_default(self, monkeypatch):
        from clio_agent.agent import ClioAgent

        monkeypatch.delenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", raising=False)
        assert ClioAgent._transient_provider_retry_delays() == (5.0, 15.0)

    def test_env(self, monkeypatch):
        from clio_agent.agent import ClioAgent

        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "1,2,3")
        assert ClioAgent._transient_provider_retry_delays() == (1.0, 2.0, 3.0)

    def test_disable_sentinel(self, monkeypatch):
        from clio_agent.agent import ClioAgent

        for token in ("false", "off", "none", "disabled"):
            monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", token)
            assert ClioAgent._transient_provider_retry_delays() == ()

    def test_empty_env_disables(self, monkeypatch):
        # Legacy contract: a SET-but-empty env var explicitly disables retries
        # (conf alone would treat it as unset and re-enable the 5s/15s default).
        from clio_agent.agent import ClioAgent

        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "")
        assert ClioAgent._transient_provider_retry_delays() == ()
        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "   ")
        assert ClioAgent._transient_provider_retry_delays() == ()

    def test_file_wins_over_empty_env(self, monkeypatch, tmp_path):
        # File -> env -> default precedence still holds: a file value beats the
        # empty-env disable sentinel.
        from clio_agent.agent import ClioAgent

        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "")
        _write_user_config(
            monkeypatch,
            tmp_path,
            "limits:\n  transient_provider_retry_delays:\n    - 7\n    - 8\n",
        )
        assert ClioAgent._transient_provider_retry_delays() == (7.0, 8.0)

    def test_clamped_and_garbage_dropped(self, monkeypatch):
        from clio_agent.agent import ClioAgent

        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "999, junk, 3")
        assert ClioAgent._transient_provider_retry_delays() == (60.0, 3.0)

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.agent import ClioAgent

        monkeypatch.setenv("CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS", "1,2")
        _write_user_config(
            monkeypatch,
            tmp_path,
            "limits:\n  transient_provider_retry_delays:\n    - 7\n    - 8\n",
        )
        assert ClioAgent._transient_provider_retry_delays() == (7.0, 8.0)


class TestFilePolicyMappingSymlinks:
    """``FileAccessPolicy.from_mapping`` keeps the injected-mapping path but routes
    the truthy through ``conf.as_bool`` (one truthy rule); it does NOT read conf."""

    def test_truthy_via_as_bool(self):
        from clio_agent.tools.file_policy import FileAccessPolicy

        assert FileAccessPolicy.from_mapping({"CLIO_ALLOW_SYMLINKS": "on"}).allow_symlinks is True
        assert FileAccessPolicy.from_mapping({"CLIO_ALLOW_SYMLINKS": "0"}).allow_symlinks is False
        assert FileAccessPolicy.from_mapping({}).allow_symlinks is False

    def test_garbage_is_false(self):
        from clio_agent.tools.file_policy import FileAccessPolicy

        assert (
            FileAccessPolicy.from_mapping({"CLIO_ALLOW_SYMLINKS": "banana"}).allow_symlinks is False
        )

    def test_mapping_ignores_config_file(self, monkeypatch, tmp_path):
        from clio_agent.tools.file_policy import FileAccessPolicy

        # A config file that says True must NOT leak into the injected-mapping path.
        _write_user_config(
            monkeypatch, tmp_path, "tools:\n  file_policy:\n    allow_symlinks: true\n"
        )
        assert FileAccessPolicy.from_mapping({}).allow_symlinks is False


class TestStopSequencesOverride:
    """``lm.stop_sequences`` — env stays ``||``-joined; the file layer takes a list."""

    @staticmethod
    def _cfg():
        from types import SimpleNamespace

        # lm_studio + qwopus so _reasoning_model_capability() is True and the stop
        # override branch runs.
        return SimpleNamespace(
            provider="lm_studio",
            model="qwopus",
            is_reasoning=True,
            top_p=None,
            presence_penalty=None,
            top_k=None,
            min_p=None,
            codex_transport="",
            claude_code_transport="",
        )

    def test_env_double_pipe_split(self, monkeypatch):
        from clio_agent.config import _provider_lm_kwargs

        monkeypatch.setenv("CLIO_LM_STOP_SEQUENCES", "</s>||STOP")
        assert _provider_lm_kwargs(self._cfg())["stop"] == ["</s>", "STOP"]

    def test_default_when_unset(self, monkeypatch):
        from clio_agent.config import _provider_lm_kwargs

        monkeypatch.delenv("CLIO_LM_STOP_SEQUENCES", raising=False)
        stop = _provider_lm_kwargs(self._cfg())["stop"]
        assert stop == [
            "[[ ## observation",
            "[[ ## thought_",
            "[[ ## tool_name_",
            "[[ ## tool_args_",
        ]

    def test_file_list_wins(self, monkeypatch, tmp_path):
        from clio_agent.config import _provider_lm_kwargs

        monkeypatch.setenv("CLIO_LM_STOP_SEQUENCES", "</s>")
        _write_user_config(monkeypatch, tmp_path, "lm:\n  stop_sequences:\n    - AAA\n    - BBB\n")
        assert _provider_lm_kwargs(self._cfg())["stop"] == ["AAA", "BBB"]


class TestArcCorePort:
    """``arc.core_port`` / ``CLIO_CORE_PORT`` — runtime liveness-probe port."""

    def test_env(self, monkeypatch):
        from clio_agent.arc.storage import _resolve_runtime_port

        monkeypatch.setenv("CLIO_CORE_PORT", "4567")
        assert _resolve_runtime_port("") == 4567

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import _resolve_runtime_port

        monkeypatch.setenv("CLIO_CORE_PORT", "4567")
        _write_user_config(monkeypatch, tmp_path, "arc:\n  core_port: 7654\n")
        assert _resolve_runtime_port("") == 7654


class TestArcServerConf:
    """``arc.server_conf`` / ``CLIO_SERVER_CONF`` — clio-core config file path."""

    @staticmethod
    def _port_yaml(tmp_path, name: str, port: int) -> str:
        p = tmp_path / name
        p.write_text(f"networking:\n  port: {port}\n", encoding="utf-8")
        return str(p)

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import _resolve_runtime_port

        monkeypatch.delenv("CLIO_CORE_PORT", raising=False)
        monkeypatch.delenv("CHI_SERVER_CONF", raising=False)
        monkeypatch.setenv("CLIO_SERVER_CONF", self._port_yaml(tmp_path, "env.yaml", 6111))
        assert _resolve_runtime_port("") == 6111

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import _resolve_runtime_port

        monkeypatch.delenv("CLIO_CORE_PORT", raising=False)
        monkeypatch.delenv("CHI_SERVER_CONF", raising=False)
        monkeypatch.setenv("CLIO_SERVER_CONF", self._port_yaml(tmp_path, "env.yaml", 6111))
        file_conf = Path(self._port_yaml(tmp_path, "file.yaml", 6222)).as_posix()
        _write_user_config(monkeypatch, tmp_path, f"arc:\n  server_conf: {file_conf}\n")
        assert _resolve_runtime_port("") == 6222


class TestArcStore:
    """``arc.store`` / ``CLIO_ARC_STORE`` — ARC backend selection."""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import LocalFSStore, make_arc_store

        # Drop the fixture's file-layer ``arc.store`` so the env is the real source
        # under test (file > env; a bare setenv would otherwise be shadowed).
        from tests._config_layer import delete_config

        delete_config("arc.store")
        monkeypatch.setenv("CLIO_ARC_STORE", "local")
        assert isinstance(make_arc_store(data_dir=tmp_path / "arc"), LocalFSStore)

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import LocalFSStore, make_arc_store

        # env says cte (which would raise without clio-core); file layer wins.
        monkeypatch.setenv("CLIO_ARC_STORE", "cte")
        _write_user_config(monkeypatch, tmp_path, "arc:\n  store: local\n")
        assert isinstance(make_arc_store(data_dir=tmp_path / "arc"), LocalFSStore)

    def test_explicit_backend_arg_beats_config(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import LocalFSStore, make_arc_store

        monkeypatch.setenv("CLIO_ARC_STORE", "cte")
        assert isinstance(make_arc_store(backend="local", data_dir=tmp_path / "arc"), LocalFSStore)

    def test_unknown_backend_fails_loud(self, monkeypatch, tmp_path):
        from clio_agent.arc.storage import make_arc_store

        # The autouse fixture pins ``arc.store: local`` in the config-FILE layer
        # (file > env), so a ``setenv`` here could never reach the resolver. Express
        # the fail-loud contract at the file layer instead: an unknown backend name
        # in config.yaml must still raise (#985 residual re-expression).
        from tests._config_layer import set_config

        set_config("arc.store", "banana")
        with pytest.raises(ValueError, match="banana"):
            make_arc_store(data_dir=tmp_path / "arc")


class TestArcStoreConfig:
    """``arc.store_config`` / ``CLIO_ARC_STORE_CONFIG`` — CTE config path."""

    @pytest.fixture()
    def _stub_clio_core(self, monkeypatch):
        from clio_agent.arc import storage

        captured: dict[str, str] = {}

        class _StubClioCore:
            def __init__(self, config_path: str = "") -> None:
                captured["config_path"] = config_path

        monkeypatch.setattr(storage, "ClioCoreStore", _StubClioCore)
        return captured

    def test_env(self, monkeypatch, tmp_path, _stub_clio_core):
        from clio_agent.arc.storage import make_arc_store

        # Drop the fixture's file-pinned ``arc.store: local`` so the cte branch is
        # reachable; the SUBJECT here is the ``store_config`` env resolution (#985).
        from tests._config_layer import delete_config

        delete_config("arc.store")
        monkeypatch.setenv("CLIO_ARC_STORE", "cte")
        monkeypatch.setenv("CLIO_ARC_STORE_CONFIG", str(tmp_path / "env-cte.yaml"))
        make_arc_store(data_dir=tmp_path / "arc")
        assert _stub_clio_core["config_path"] == str(tmp_path / "env-cte.yaml")

    def test_file_wins(self, monkeypatch, tmp_path, _stub_clio_core):
        from clio_agent.arc.storage import make_arc_store

        monkeypatch.setenv("CLIO_ARC_STORE", "cte")
        monkeypatch.setenv("CLIO_ARC_STORE_CONFIG", str(tmp_path / "env-cte.yaml"))
        file_cfg = (tmp_path / "file-cte.yaml").as_posix()
        _write_user_config(monkeypatch, tmp_path, f"arc:\n  store_config: {file_cfg}\n")
        make_arc_store(data_dir=tmp_path / "arc")
        assert _stub_clio_core["config_path"] == file_cfg


class TestSessionsPath:
    """``paths.sessions`` / ``CLIO_SESSIONS_PATH`` — gact session store file."""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.gact.sessions import _default_store_path

        monkeypatch.setenv("CLIO_SESSIONS_PATH", str(tmp_path / "sessions.json"))
        assert _default_store_path() == tmp_path / "sessions.json"

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.sessions import _default_store_path

        monkeypatch.setenv("CLIO_SESSIONS_PATH", str(tmp_path / "env.json"))
        file_path = (tmp_path / "file.json").as_posix()
        _write_user_config(monkeypatch, tmp_path, f"paths:\n  sessions: {file_path}\n")
        assert _default_store_path() == tmp_path / "file.json"


class TestModelDbPath:
    """``paths.model_db`` / ``CLIO_MODEL_DB`` — handshake model-limits DB file."""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.providers.handshake.sources.db import db_path

        monkeypatch.setenv("CLIO_MODEL_DB", str(tmp_path / "db.json"))
        assert db_path() == tmp_path / "db.json"

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.providers.handshake.sources.db import db_path

        monkeypatch.setenv("CLIO_MODEL_DB", str(tmp_path / "env-db.json"))
        file_path = (tmp_path / "file-db.json").as_posix()
        _write_user_config(monkeypatch, tmp_path, f"paths:\n  model_db: {file_path}\n")
        assert db_path() == tmp_path / "file-db.json"


class TestWebDir:
    """``paths.web_dir`` / ``CLIO_WEB_DIR`` — optional web-UI bundle directory."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.app import _web_dir

        monkeypatch.delenv("CLIO_WEB_DIR", raising=False)
        assert _web_dir() == ""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.gact.app import _web_dir

        monkeypatch.setenv("CLIO_WEB_DIR", str(tmp_path / "web"))
        assert _web_dir() == str(tmp_path / "web")

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.app import _web_dir

        monkeypatch.setenv("CLIO_WEB_DIR", str(tmp_path / "env-web"))
        file_dir = (tmp_path / "file-web").as_posix()
        _write_user_config(monkeypatch, tmp_path, f"paths:\n  web_dir: {file_dir}\n")
        assert _web_dir() == file_dir


class TestSseWireTap:
    """``debug.sse_wire_tap`` / ``CLIO_SSE_WIRE_TAP`` — raw SSE byte tap."""

    def test_default_no_write(self, monkeypatch, tmp_path):
        from clio_agent.gact.routes.misc import _sse_wire_tap

        monkeypatch.delenv("CLIO_SSE_WIRE_TAP", raising=False)
        monkeypatch.delenv("CLIO_SSE_EVENT_LOG", raising=False)
        _sse_wire_tap("sess", b"data: x\n\n", None)
        # No tap/log files appear (tmp_path holds only the conftest xdg dir).
        assert [p for p in tmp_path.iterdir() if p.name != "xdg"] == []

    def test_env_writes_frames(self, monkeypatch, tmp_path):
        from clio_agent.gact.routes.misc import _sse_wire_tap

        tap = tmp_path / "tap.bin"
        monkeypatch.setenv("CLIO_SSE_WIRE_TAP", str(tap))
        monkeypatch.delenv("CLIO_SSE_EVENT_LOG", raising=False)
        _sse_wire_tap("sess", b"data: x\n\n", None)
        assert tap.read_bytes() == b"data: x\n\n"

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.routes.misc import _sse_wire_tap

        env_tap = tmp_path / "env-tap.bin"
        file_tap = tmp_path / "file-tap.bin"
        monkeypatch.setenv("CLIO_SSE_WIRE_TAP", str(env_tap))
        _write_user_config(
            monkeypatch, tmp_path, f"debug:\n  sse_wire_tap: {file_tap.as_posix()}\n"
        )
        _sse_wire_tap("sess", b"frame", None)
        assert file_tap.read_bytes() == b"frame"
        assert not env_tap.exists()


class TestSseEventLog:
    """``debug.sse_event_log`` / ``CLIO_SSE_EVENT_LOG`` — per-event JSONL log."""

    @staticmethod
    def _event():
        from clio_agent.gact.events import Event

        return Event(type="turn.completed", session_id="sess", payload={"a": 1})

    def test_env(self, monkeypatch, tmp_path):
        import json

        from clio_agent.gact.routes.misc import _sse_wire_tap

        monkeypatch.delenv("CLIO_SSE_WIRE_TAP", raising=False)
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("CLIO_SSE_EVENT_LOG", str(log))
        _sse_wire_tap("sess", b"frame", self._event())
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert row["event_type"] == "turn.completed"
        assert row["payload_keys"] == ["a"]

    def test_file_wins(self, monkeypatch, tmp_path):
        import json

        from clio_agent.gact.routes.misc import _sse_wire_tap

        monkeypatch.delenv("CLIO_SSE_WIRE_TAP", raising=False)
        env_log = tmp_path / "env.jsonl"
        file_log = tmp_path / "file.jsonl"
        monkeypatch.setenv("CLIO_SSE_EVENT_LOG", str(env_log))
        _write_user_config(
            monkeypatch, tmp_path, f"debug:\n  sse_event_log: {file_log.as_posix()}\n"
        )
        _sse_wire_tap("sess", b"frame", self._event())
        assert not env_log.exists()
        row = json.loads(file_log.read_text(encoding="utf-8").splitlines()[0])
        assert row["session_id"] == "sess"


class TestMemprofOut:
    """``debug.memprof_out`` / ``CLIO_DEBUG_MEMPROF_OUT`` — dump file stem."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.diagnostics import _memprof_out

        monkeypatch.delenv("CLIO_DEBUG_MEMPROF_OUT", raising=False)
        assert _memprof_out() == ""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.gact.diagnostics import _memprof_out

        monkeypatch.setenv("CLIO_DEBUG_MEMPROF_OUT", str(tmp_path / "memprof"))
        assert _memprof_out() == str(tmp_path / "memprof")

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.diagnostics import _memprof_out

        monkeypatch.setenv("CLIO_DEBUG_MEMPROF_OUT", str(tmp_path / "env-memprof"))
        file_stem = (tmp_path / "file-memprof").as_posix()
        _write_user_config(monkeypatch, tmp_path, f"debug:\n  memprof_out: {file_stem}\n")
        assert _memprof_out() == file_stem


class TestMemprofFrames:
    """``debug.memprof_frames`` / ``CLIO_DEBUG_MEMPROF_FRAMES`` — stack depth."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.diagnostics import _memprof_frames

        monkeypatch.delenv("CLIO_DEBUG_MEMPROF_FRAMES", raising=False)
        assert _memprof_frames() == 20

    def test_env(self, monkeypatch):
        from clio_agent.gact.diagnostics import _memprof_frames

        monkeypatch.setenv("CLIO_DEBUG_MEMPROF_FRAMES", "5")
        assert _memprof_frames() == 5

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.diagnostics import _memprof_frames

        monkeypatch.setenv("CLIO_DEBUG_MEMPROF_FRAMES", "5")
        _write_user_config(monkeypatch, tmp_path, "debug:\n  memprof_frames: 33\n")
        assert _memprof_frames() == 33


_TRACE_FACTORY_MODULE = """\
class _Backend:
    name = "conf-mig-trace-backend"

    def __init__(self, default_root, config):
        self.default_root = default_root
        self.config = config

    def emit(self, event):
        pass


def make(default_root, config):
    return _Backend(default_root, config)
"""


def _install_trace_factory_module(tmp_path, monkeypatch) -> str:
    """Write an importable trace-factory module and return its factory path."""
    mod_dir = tmp_path / "trace_factory_mods"
    mod_dir.mkdir(exist_ok=True)
    (mod_dir / "conf_mig_trace_factory.py").write_text(_TRACE_FACTORY_MODULE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(mod_dir))
    return "conf_mig_trace_factory:make"


class TestSemanticTraceFactory:
    """``trace.semantic_factory`` + ``trace.semantic_config`` (factory backend)."""

    def test_env(self, monkeypatch, tmp_path):
        from clio_agent.gact.semantic_events import build_trace_backend

        factory_path = _install_trace_factory_module(tmp_path, monkeypatch)
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "factory")
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_FACTORY", factory_path)
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_CONFIG", '{"a": 1}')
        backend = build_trace_backend(tmp_path)
        assert backend.name == "conf-mig-trace-backend"
        assert backend.config == {"a": 1}
        assert backend.default_root == tmp_path

    def test_missing_factory_raises(self, monkeypatch):
        from clio_agent.gact.semantic_events import build_trace_backend

        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "factory")
        monkeypatch.delenv("CLIO_SEMANTIC_TRACE_FACTORY", raising=False)
        with pytest.raises(ValueError, match="CLIO_SEMANTIC_TRACE_FACTORY"):
            build_trace_backend(Path("."))

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.semantic_events import build_trace_backend

        factory_path = _install_trace_factory_module(tmp_path, monkeypatch)
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_BACKEND", "factory")
        # env points at a factory that does not exist; the file layer wins with
        # the real one, and the file config payload wins over the env payload.
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_FACTORY", "no_such_module:make")
        monkeypatch.setenv("CLIO_SEMANTIC_TRACE_CONFIG", '{"a": 1}')
        _write_user_config(
            monkeypatch,
            tmp_path,
            f"trace:\n  semantic_factory: {factory_path}\n  semantic_config: '{{\"b\": 2}}'\n",
        )
        backend = build_trace_backend(tmp_path)
        assert backend.name == "conf-mig-trace-backend"
        assert backend.config == {"b": 2}


class TestLmStudioFlashAttention:
    """``lm.lmstudio_flash_attention`` / ``CLIO_LMSTUDIO_FLASH_ATTENTION``."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.routes.providers import _lmstudio_flash_attention_enabled

        monkeypatch.delenv("CLIO_LMSTUDIO_FLASH_ATTENTION", raising=False)
        assert _lmstudio_flash_attention_enabled() is True

    def test_env(self, monkeypatch):
        from clio_agent.gact.routes.providers import _lmstudio_flash_attention_enabled

        monkeypatch.setenv("CLIO_LMSTUDIO_FLASH_ATTENTION", "0")
        assert _lmstudio_flash_attention_enabled() is False

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.routes.providers import _lmstudio_flash_attention_enabled

        monkeypatch.setenv("CLIO_LMSTUDIO_FLASH_ATTENTION", "1")
        _write_user_config(monkeypatch, tmp_path, "lm:\n  lmstudio_flash_attention: false\n")
        assert _lmstudio_flash_attention_enabled() is False


class TestDisableJsonAdapterFallback:
    """``lm.disable_json_adapter_fallback`` / ``CLIO_DISABLE_JSON_ADAPTER_FALLBACK``."""

    @staticmethod
    def _remote_cfg():
        from types import SimpleNamespace

        # A remote (non-local) backend keeps the JSON-adapter fallback ON unless
        # the knob disables it.
        return SimpleNamespace(
            provider="openai",
            api_base="https://api.openai.com/v1",
            model="gpt-4o",
            is_reasoning=False,
        )

    def test_default(self, monkeypatch):
        from clio_agent.config import create_chat_adapter

        monkeypatch.delenv("CLIO_DISABLE_JSON_ADAPTER_FALLBACK", raising=False)
        monkeypatch.delenv("CLIO_LM_GUIDED_OUTPUT", raising=False)
        adapter = create_chat_adapter(self._remote_cfg())
        assert adapter.use_json_adapter_fallback is True

    def test_env(self, monkeypatch):
        from clio_agent.config import create_chat_adapter

        monkeypatch.setenv("CLIO_DISABLE_JSON_ADAPTER_FALLBACK", "1")
        monkeypatch.delenv("CLIO_LM_GUIDED_OUTPUT", raising=False)
        adapter = create_chat_adapter(self._remote_cfg())
        assert adapter.use_json_adapter_fallback is False

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.config import create_chat_adapter

        monkeypatch.setenv("CLIO_DISABLE_JSON_ADAPTER_FALLBACK", "1")
        monkeypatch.delenv("CLIO_LM_GUIDED_OUTPUT", raising=False)
        _write_user_config(monkeypatch, tmp_path, "lm:\n  disable_json_adapter_fallback: false\n")
        adapter = create_chat_adapter(self._remote_cfg())
        assert adapter.use_json_adapter_fallback is True


class TestDumpUnparseable:
    """``debug.dump_unparseable`` / ``CLIO_DUMP_UNPARSEABLE`` — diagnostic dump path."""

    @staticmethod
    def _dump():
        from clio_agent.config import _dump_unparseable_completion

        _dump_unparseable_completion(object, "raw completion", "answer", "value", "boom")

    def test_default_no_write(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLIO_DUMP_UNPARSEABLE", raising=False)
        self._dump()
        assert [p for p in tmp_path.iterdir() if p.name != "xdg"] == []

    def test_env(self, monkeypatch, tmp_path):
        import json

        dump = tmp_path / "dump.jsonl"
        monkeypatch.setenv("CLIO_DUMP_UNPARSEABLE", str(dump))
        self._dump()
        row = json.loads(dump.read_text(encoding="utf-8").splitlines()[0])
        assert row["failing_field"] == "answer"
        assert row["raw_completion"] == "raw completion"

    def test_file_wins(self, monkeypatch, tmp_path):
        env_dump = tmp_path / "env.jsonl"
        file_dump = tmp_path / "file.jsonl"
        monkeypatch.setenv("CLIO_DUMP_UNPARSEABLE", str(env_dump))
        _write_user_config(
            monkeypatch, tmp_path, f"debug:\n  dump_unparseable: {file_dump.as_posix()}\n"
        )
        self._dump()
        assert file_dump.exists()
        assert not env_dump.exists()


class TestCaptureReasoning:
    """``runtime.capture_reasoning`` / ``CLIO_CAPTURE_REASONING`` (default on)."""

    def test_default(self, monkeypatch):
        from clio_agent.gact.usage import _capture_reasoning_enabled

        monkeypatch.delenv("CLIO_CAPTURE_REASONING", raising=False)
        assert _capture_reasoning_enabled() is True

    def test_env(self, monkeypatch):
        from clio_agent.gact.usage import _capture_reasoning_enabled

        monkeypatch.setenv("CLIO_CAPTURE_REASONING", "0")
        assert _capture_reasoning_enabled() is False

    def test_file_wins(self, monkeypatch, tmp_path):
        from clio_agent.gact.usage import _capture_reasoning_enabled

        monkeypatch.setenv("CLIO_CAPTURE_REASONING", "1")
        _write_user_config(monkeypatch, tmp_path, "runtime:\n  capture_reasoning: false\n")
        assert _capture_reasoning_enabled() is False


class TestDisableDefaultRegistryBootstrap:
    """``agents.disable_default_registry_bootstrap`` / env — bootstrap gate."""

    @staticmethod
    def _dirs(tmp_path):
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        home.mkdir()
        cwd.mkdir()
        return home, cwd

    def test_env_disables(self, monkeypatch, tmp_path):
        from clio_agent.gact.agent_blueprints import ensure_default_registry_bootstrap

        monkeypatch.setenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", "1")
        home, cwd = self._dirs(tmp_path)
        assert ensure_default_registry_bootstrap(home=home, cwd=cwd) == ""
        # Disabled means NO install activity under the injected home.
        assert list(home.rglob("*")) == []

    def test_file_disables(self, monkeypatch, tmp_path):
        from clio_agent.gact.agent_blueprints import ensure_default_registry_bootstrap

        monkeypatch.delenv("CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP", raising=False)
        _write_user_config(
            monkeypatch, tmp_path, "agents:\n  disable_default_registry_bootstrap: true\n"
        )
        home, cwd = self._dirs(tmp_path)
        assert ensure_default_registry_bootstrap(home=home, cwd=cwd) == ""
        assert list(home.rglob("*")) == []
