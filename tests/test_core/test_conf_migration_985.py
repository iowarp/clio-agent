"""#985 moves 1+2 — config-first migration round-trips, via ConfigStore injection.

Each knob migrated in this slice resolves file → env → default through
``clio_agent.conf``. Per the #985 spirit these tests vary CONFIG explicitly through
an injected :class:`~clio_agent.conf.ConfigStore` (hermetic user YAML + env map)
rather than mutating ambient process env with ``monkeypatch.setenv`` — the store IS
the configuration surface under test.

Covered knobs:

* the 11 ``gact.ledger_retention.*`` bounds (``gact/runtime/retention.py``);
* ``trace.detail_level`` (``gact/app.py``);
* ``paths.data_dir`` and ``runtime.api_base`` (``runtime/status.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from clio_agent import conf, paths
from clio_agent.conf import ConfigStore


def _store(
    tmp_path: Path,
    *,
    user: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> ConfigStore:
    """Build a hermetic ConfigStore with an injected user config dict + env map."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    user_dir = paths.user_config_dir_for(home, env or {})
    user_dir.mkdir(parents=True, exist_ok=True)
    (cwd / ".clio").mkdir(parents=True, exist_ok=True)
    if user is not None:
        (user_dir / "config.yaml").write_text(yaml.safe_dump(user), encoding="utf-8")
    return ConfigStore(home=home, cwd=cwd, env=env if env is not None else {})


def _nest(dotted: str, value: Any) -> dict[str, Any]:
    """Build a nested dict from a dotted path (``a.b.c`` -> ``{a:{b:{c: value}}}``)."""
    parts = dotted.split(".")
    node: Any = value
    for part in reversed(parts):
        node = {part: node}
    return node


# --------------------------------------------------------------------------- #
# Move 1 — the 11 ledger-retention bounds
# --------------------------------------------------------------------------- #

# (env var, config subkey under gact.ledger_retention, ledger name, LedgerBound attr,
#  in-code default)
_LEDGER_KNOBS = [
    ("CLIO_LEDGER_COMMAND_AUDIT_MAX", "command_audit.max", "command_audit", "max_entries", 2000),
    (
        "CLIO_LEDGER_MEMORY_TOOL_AUDIT_MAX",
        "memory_tool_audit.max",
        "memory_tool_audit",
        "max_entries",
        2000,
    ),
    ("CLIO_LEDGER_CONTEXT_FRAMES_MAX", "context_frames.max", "context_frames", "max_entries", 200),
    ("CLIO_LEDGER_PENDING_DIFFS_MAX", "pending_diffs.max", "pending_diffs", "max_entries", 500),
    ("CLIO_LEDGER_PENDING_DIFFS_HARD", "pending_diffs.hard", "pending_diffs", "hard_cap", 1000),
    ("CLIO_LEDGER_PERMISSIONS_MAX", "permissions.max", "permissions", "max_entries", 2000),
    ("CLIO_LEDGER_PERMISSIONS_HARD", "permissions.hard", "permissions", "hard_cap", 4000),
    ("CLIO_LEDGER_TURN_ATTEMPTS_MAX", "turn_attempts.max", "turn_attempts", "max_entries", 2000),
    ("CLIO_LEDGER_TURN_ATTEMPTS_HARD", "turn_attempts.hard", "turn_attempts", "hard_cap", 4000),
    ("CLIO_LEDGER_SHARED_TOKENS_MAX", "shared_tokens.max", "shared_tokens", "max_entries", 5000),
    ("CLIO_LEDGER_SHARED_TOKENS_HARD", "shared_tokens.hard", "shared_tokens", "hard_cap", 10000),
]


def _bound_value(bounds: dict, ledger: str, attr: str) -> int:
    bound = bounds[ledger]
    return getattr(bound, attr) if attr != "hard_cap" else bound.effective_hard_cap


@pytest.mark.parametrize("env,subkey,ledger,attr,default", _LEDGER_KNOBS)
def test_ledger_bound_default(monkeypatch, tmp_path, env, subkey, ledger, attr, default):
    """Absent from file and env → the documented in-code default."""
    from clio_agent.gact.runtime import retention

    monkeypatch.setattr(conf, "_STORE", _store(tmp_path))
    bounds = retention.build_ledger_bounds()
    assert _bound_value(bounds, ledger, attr) == default


@pytest.mark.parametrize("env,subkey,ledger,attr,default", _LEDGER_KNOBS)
def test_ledger_bound_env_override(monkeypatch, tmp_path, env, subkey, ledger, attr, default):
    """The env var overrides the default (file layer absent)."""
    from clio_agent.gact.runtime import retention

    monkeypatch.setattr(conf, "_STORE", _store(tmp_path, env={env: "123"}))
    bounds = retention.build_ledger_bounds()
    assert _bound_value(bounds, ledger, attr) == 123


@pytest.mark.parametrize("env,subkey,ledger,attr,default", _LEDGER_KNOBS)
def test_ledger_bound_file_wins_over_env(monkeypatch, tmp_path, env, subkey, ledger, attr, default):
    """The config file value wins over the env var (conf precedence)."""
    from clio_agent.gact.runtime import retention

    user = {"gact": {"ledger_retention": _nest(subkey, 456)}}
    monkeypatch.setattr(conf, "_STORE", _store(tmp_path, user=user, env={env: "123"}))
    bounds = retention.build_ledger_bounds()
    assert _bound_value(bounds, ledger, attr) == 456


@pytest.mark.parametrize("env,subkey,ledger,attr,default", _LEDGER_KNOBS)
def test_ledger_bound_invalid_falls_back(monkeypatch, tmp_path, env, subkey, ledger, attr, default):
    """A non-integer or non-positive value falls back to the default (typed reason)."""
    from clio_agent.gact.runtime import retention

    monkeypatch.setattr(conf, "_STORE", _store(tmp_path, env={env: "not-a-number"}))
    assert _bound_value(retention.build_ledger_bounds(), ledger, attr) == default
    monkeypatch.setattr(conf, "_STORE", _store(tmp_path, env={env: "0"}))
    assert _bound_value(retention.build_ledger_bounds(), ledger, attr) == default


# --------------------------------------------------------------------------- #
# Move 1 — trace.detail_level
# --------------------------------------------------------------------------- #


class TestTraceDetailLevel:
    """``trace.detail_level`` / ``CLIO_SEMANTIC_TRACE_DETAIL`` (sibling of trace.backend)."""

    def test_default(self, monkeypatch, tmp_path):
        from clio_agent.gact.app import _semantic_trace_detail_level
        from clio_agent.gact.semantic_events import DEFAULT_DETAIL_LEVEL

        monkeypatch.setattr(conf, "_STORE", _store(tmp_path))
        assert _semantic_trace_detail_level() == DEFAULT_DETAIL_LEVEL

    def test_env_override(self, monkeypatch, tmp_path):
        from clio_agent.gact.app import _semantic_trace_detail_level

        monkeypatch.setattr(
            conf, "_STORE", _store(tmp_path, env={"CLIO_SEMANTIC_TRACE_DETAIL": "verbose"})
        )
        assert _semantic_trace_detail_level() == "verbose"

    def test_file_wins_over_env(self, monkeypatch, tmp_path):
        from clio_agent.gact.app import _semantic_trace_detail_level

        store = _store(
            tmp_path,
            user={"trace": {"detail_level": "minimal"}},
            env={"CLIO_SEMANTIC_TRACE_DETAIL": "verbose"},
        )
        monkeypatch.setattr(conf, "_STORE", store)
        assert _semantic_trace_detail_level() == "minimal"


# --------------------------------------------------------------------------- #
# Move 1 — paths.data_dir + runtime.api_base (RuntimeProbe, direct store injection)
# --------------------------------------------------------------------------- #


class TestStatusDataDir:
    """``paths.data_dir`` / ``CLIO_DATA_DIR`` via RuntimeProbe (config_store injected)."""

    def test_default(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        probe = RuntimeProbe(env={}, config_store=_store(tmp_path))
        base, source = probe._data_dir()
        assert base == Path(".clio/agent")
        assert source == "default:.clio/agent"

    def test_env_override(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        env = {"CLIO_DATA_DIR": str(tmp_path / "from_env")}
        probe = RuntimeProbe(env=env, config_store=_store(tmp_path, env=env))
        base, source = probe._data_dir()
        assert base == tmp_path / "from_env"
        assert source == "env:CLIO_DATA_DIR"

    def test_file_wins_over_env(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        env = {"CLIO_DATA_DIR": str(tmp_path / "from_env")}
        store = _store(tmp_path, user={"paths": {"data_dir": str(tmp_path / "from_file")}}, env=env)
        probe = RuntimeProbe(env=env, config_store=store)
        base, source = probe._data_dir()
        assert base == tmp_path / "from_file"
        assert source == "config:paths.data_dir"


class TestStatusApiBase:
    """``runtime.api_base`` / ``CLIO_API_BASE`` via RuntimeProbe (config_store injected)."""

    def test_default_empty(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        probe = RuntimeProbe(env={}, config_store=_store(tmp_path))
        endpoint, _ = probe._api_base()
        assert endpoint == ""

    def test_env_override(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        env = {"CLIO_API_BASE": "http://env.example:8000"}
        probe = RuntimeProbe(env=env, config_store=_store(tmp_path, env=env))
        endpoint, source = probe._api_base()
        assert endpoint == "http://env.example:8000"
        assert source == "env:CLIO_API_BASE"

    def test_file_wins_over_env(self, tmp_path):
        from clio_agent.runtime.status import RuntimeProbe

        env = {"CLIO_API_BASE": "http://env.example:8000"}
        store = _store(
            tmp_path,
            user={"runtime": {"api_base": "http://file.example:9000"}},
            env=env,
        )
        probe = RuntimeProbe(env=env, config_store=store)
        endpoint, source = probe._api_base()
        assert endpoint == "http://file.example:9000"
        assert source == "config:runtime.api_base"
