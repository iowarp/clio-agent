"""clio-relay#209 A2: config/parsing/typed-error surface (tools/relay_cli_runner.py).

Pure-unit coverage for parsing/executable-resolution, plus a subprocess-level proof
of the F5b environment allowlist against a locally-generated FAKE ``clio-relay``
executable -- no live relay, no ssh, no mocked ``subprocess`` internals. Job
execution/registry behavior (``RelayInstallJob``, ``start_relay_install_job``,
``run_bounded_relay_cli``) is covered in the sibling
``test_relay_install_jobs.py``, matching the source split.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from clio_agent.tools.relay_cli_runner import (
    RelayCliUnavailableError,
    _classify_exit_state,
    _detect_actionable_refusal,
    _subprocess_env,
    parse_relay_cli_stdout,
    resolve_relay_cli_executable,
)


@pytest.fixture
def env_echo_cli(tmp_path: Path) -> str:
    """A fake executable that echoes back exactly which env vars it received.

    Prints ``ENV:{name}={value or <unset>}`` for every name passed as an argv
    argument -- used to prove F5b's explicit allowlist against a REAL child
    process's actual environment, not an assertion about what clio-agent
    intended to pass.
    """

    py_path = tmp_path / "env_echo_cli.py"
    py_path.write_text(
        textwrap.dedent(
            """
            import os, sys
            for name in sys.argv[1:]:
                sys.stdout.write(f"ENV:{name}={os.environ.get(name, '<unset>')}\\n")
            """
        ),
        encoding="utf-8",
    )
    if sys.platform.startswith("win"):
        executable = tmp_path / "env_echo_cli.cmd"
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n', encoding="utf-8"
        )
    else:
        executable = tmp_path / "env_echo_cli"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{py_path}" "$@"\n', encoding="utf-8"
        )
        executable.chmod(0o755)
    return str(executable)


# --------------------------------------------------------------------------- #
# parse_relay_cli_stdout / declared-marker framing (F5a)
# --------------------------------------------------------------------------- #


def test_parse_relay_cli_stdout_framed_marker_lines_in_order() -> None:
    """Bootstrap's ``marker=json`` lines (a DECLARED namespace) become ordered
    typed fields; an unknown key WITHIN a declared namespace still passes its
    value through verbatim (the allowlist is on the namespace prefix, not an
    exhaustive enum of every possible marker name)."""

    text = (
        'bootstrap_preflight_json={"ok": true}\n'
        'bootstrap_target_identity_pinned={"trust": "first_use", "hostnames": ["h1"]}\n'
        "bootstrap_unknown_future_marker=some literal text\n"
    )
    fields, whole = parse_relay_cli_stdout(text)
    assert whole is None
    assert [f.key for f in fields] == [
        "bootstrap_preflight_json",
        "bootstrap_target_identity_pinned",
        "bootstrap_unknown_future_marker",
    ]
    assert [f.seq for f in fields] == [0, 1, 2]
    assert fields[0].value_json == {"ok": True}
    assert fields[1].value_json == {"trust": "first_use", "hostnames": ["h1"]}
    assert fields[2].value == "some literal text"
    assert fields[2].value_json is None


def test_parse_relay_cli_stdout_drops_non_declared_key_value_lines() -> None:
    """FAILING-FIRST (F5a): a line that merely LOOKS like ``key=value`` but is not
    under a declared clio-relay marker namespace is NEVER promoted to a receipt
    field -- proven with a planted fake secret token an env echo / ``set -x``
    trace could plausibly relay onto stdout."""

    text = (
        'bootstrap_preflight_json={"ok": true}\n'
        "FRP_TOKEN=sekrit-leaked-token-do-not-retain\n"
        "DEBUG=1\n"
        "+ export CLIO_RELAY_FRP_TOKEN=another-leaked-value\n"
    )
    fields, whole = parse_relay_cli_stdout(text)
    assert whole is None
    assert [f.key for f in fields] == ["bootstrap_preflight_json"]
    keys = {f.key for f in fields}
    assert "FRP_TOKEN" not in keys
    assert "DEBUG" not in keys
    values = {f.value for f in fields}
    assert not any("sekrit-leaked-token" in v for v in values)


def test_parse_relay_cli_stdout_whole_document_json() -> None:
    """session/relay-host commands print ONE pretty JSON document; parsed whole,
    never fragmented into (mismatching) per-line framed fields."""

    text = '{\n  "state": "ready",\n  "session_id": "s1"\n}\n'
    fields, whole = parse_relay_cli_stdout(text)
    assert fields == []
    assert whole == {"state": "ready", "session_id": "s1"}


def test_parse_relay_cli_stdout_plain_prose_yields_neither() -> None:
    """Human-readable prose (e.g. doctor's output) is retained in the tails only --
    never turned into a fabricated field or document."""

    fields, whole = parse_relay_cli_stdout("cluster demo: reachable\nworker: healthy\n")
    assert fields == []
    assert whole is None


# --------------------------------------------------------------------------- #
# _classify_exit_state (M2)
# --------------------------------------------------------------------------- #


def test_classify_exit_state_session_start_exit_2_is_handle_only() -> None:
    """FAILING-FIRST (M2): clio-relay's own documented non-failure outcome for
    ``session start`` (exit 2) maps to handle_only, never failed."""

    assert _classify_exit_state("relay_session_start", 2) == "handle_only"


def test_classify_exit_state_exit_2_elsewhere_stays_failed() -> None:
    """M2's exception is scoped to relay_session_start ONLY -- any other verb's
    exit 2 is a bare failure (no generic exit-code convention)."""

    assert _classify_exit_state("relay_cluster_bootstrap", 2) == "failed"
    assert _classify_exit_state("relay_proxy_install", 2) == "failed"


def test_classify_exit_state_zero_is_completed_regardless_of_kind() -> None:
    assert _classify_exit_state("relay_session_start", 0) == "completed"
    assert _classify_exit_state("relay_cluster_register", 0) == "completed"


# --------------------------------------------------------------------------- #
# _detect_actionable_refusal (M3: proxy-lifecycle-scoped)
# --------------------------------------------------------------------------- #


_LINGERING_STDERR = "persistent frpc proxy requires systemd user lingering (Linger=yes)\n"


def test_detect_actionable_refusal_scoped_to_proxy_kinds() -> None:
    """FAILING-FIRST (M3): the lingering-gate substring is only classified for
    proxy install/teardown kinds. The SAME substring in a bootstrap/session
    failure must NOT be mis-surfaced as a proxy-specific actionable refusal."""

    assert _detect_actionable_refusal("", _LINGERING_STDERR, kind="relay_proxy_install") is not None
    assert (
        _detect_actionable_refusal("", _LINGERING_STDERR, kind="relay_proxy_teardown") is not None
    )
    assert _detect_actionable_refusal("", _LINGERING_STDERR, kind="relay_cluster_bootstrap") is None
    assert _detect_actionable_refusal("", _LINGERING_STDERR, kind="relay_session_start") is None


# --------------------------------------------------------------------------- #
# resolve_relay_cli_executable
# --------------------------------------------------------------------------- #


def test_resolve_relay_cli_executable_prefers_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLIO_RELAY_CLI_PATH wins over a bare PATH lookup."""

    fake = tmp_path / "clio-relay-here"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(fake))
    assert resolve_relay_cli_executable() == str(fake)


def test_resolve_relay_cli_executable_unconfigured_and_absent_raises_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config override and nothing named clio-relay on PATH: typed refusal,
    never a bare FileNotFoundError from a downstream Popen."""

    monkeypatch.delenv("CLIO_RELAY_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RelayCliUnavailableError) as excinfo:
        resolve_relay_cli_executable()
    assert excinfo.value.reason == "relay_cli_unavailable"


def test_resolve_relay_cli_executable_configured_path_missing_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (M5): a CONFIGURED path that does not exist on disk (a stale
    or typo'd config value) is a typed, distinguishable refusal -- never a raw
    OSError surfacing from whichever Popen/run call happens to try it first."""

    missing = tmp_path / "does-not-exist-clio-relay"
    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(missing))
    with pytest.raises(RelayCliUnavailableError) as excinfo:
        resolve_relay_cli_executable()
    assert excinfo.value.reason == "relay_cli_configured_path_missing"
    assert excinfo.value.details["configured_path"] == str(missing)


# --------------------------------------------------------------------------- #
# _subprocess_env (F5b)
# --------------------------------------------------------------------------- #


def test_subprocess_env_is_an_allowlist_not_full_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure check: a var NOT on any allowlist never appears, even when set in the
    real process environment."""

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLIO_RELAY_API_TOKEN", "unrelated-client-side-token")
    monkeypatch.setenv("SOME_RANDOM_SECRET", "should-not-leak")
    env = _subprocess_env("relay_cluster_register")
    assert env.get("PATH") == "/usr/bin"
    assert "CLIO_RELAY_API_TOKEN" not in env
    assert "SOME_RANDOM_SECRET" not in env


def test_subprocess_env_grants_frp_secrets_only_to_proxy_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F5b: 'enumerated per tool' -- only relay-host install/teardown-proxy get
    the frp/stcp secret env vars; every other verb does not."""

    monkeypatch.setenv("CLIO_RELAY_FRP_TOKEN", "frp-secret")
    monkeypatch.setenv("CLIO_RELAY_STCP_SECRET", "stcp-secret")
    assert _subprocess_env("relay_proxy_install")["CLIO_RELAY_FRP_TOKEN"] == "frp-secret"
    assert _subprocess_env("relay_proxy_teardown")["CLIO_RELAY_STCP_SECRET"] == "stcp-secret"
    assert "CLIO_RELAY_FRP_TOKEN" not in _subprocess_env("relay_cluster_bootstrap")
    assert "CLIO_RELAY_FRP_TOKEN" not in _subprocess_env("relay_cluster_register")
    assert "CLIO_RELAY_FRP_TOKEN" not in _subprocess_env("relay_session_start")


def test_planted_secret_does_not_reach_a_real_child_process(
    env_echo_cli: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (F5b): proven on a REAL subprocess, not just the dict this
    module builds -- a planted secret in clio-agent's own process environment
    must not be observable inside the child at all."""

    monkeypatch.setenv("PLANTED_SECRET", "leak-me-if-you-can")
    env = _subprocess_env("relay_cluster_register")
    completed = subprocess.run(
        [env_echo_cli, "PLANTED_SECRET", "PATH"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert "ENV:PLANTED_SECRET=<unset>" in completed.stdout
