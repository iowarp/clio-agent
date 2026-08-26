"""clio-relay#209 A2: the local subprocess runner (tools/relay_cli_runner.py).

Pure-unit coverage for parsing/state-derivation/executable-resolution, plus a few
subprocess-level tests exercising :func:`run_bounded_relay_cli` and
:func:`start_relay_install_job` directly (bypassing the curated tool surface) against
a locally-generated FAKE ``clio-relay`` executable -- no live relay, no ssh, no
mocked ``subprocess`` internals. The fake executable is a thin OS dispatcher (a
``.cmd`` on Windows, a shebang script on POSIX) around a small, fully portable Python
script whose behavior is driven by a JSON scenario file keyed on the invoked argv, so
one fixture serves every test case.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from clio_agent.tools.relay_cli_runner import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_NEEDS_USER_ATTENTION,
    STATE_RUNNING,
    RelayCliUnavailableError,
    RelayInstallJob,
    RelayInstallJobRegistry,
    effective_job_state,
    parse_relay_cli_stdout,
    resolve_relay_cli_executable,
    run_bounded_relay_cli,
    start_relay_install_job,
)

ScenarioSetter = Callable[[dict[str, dict[str, Any]]], None]


@pytest.fixture
def fake_relay_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ScenarioSetter]:
    """Build a fake ``clio-relay`` executable and return ``(path, set_scenarios)``.

    ``set_scenarios`` writes the JSON the fake CLI reads at invocation time, keyed
    by the two-token argv prefix (``"cluster bootstrap"``), falling back to the
    one-token prefix (``"doctor"``) then ``"default"`` -- letting one process fixture
    serve every clio-relay verb with independently scripted stdout/stderr/exit_code/
    an optional startup sleep (for timeout/needs_user_attention tests).
    """

    py_path = tmp_path / "fake_relay_cli.py"
    py_path.write_text(
        textwrap.dedent(
            """
            import json, os, sys, time

            def main() -> int:
                config_path = os.environ.get("FAKE_RELAY_CONFIG_FILE")
                scenarios = {}
                if config_path and os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as fh:
                        scenarios = json.load(fh)
                argv = sys.argv[1:]
                key_two = " ".join(argv[:2]) if len(argv) >= 2 else ""
                key_one = argv[0] if argv else ""
                scenario = scenarios.get(key_two) or scenarios.get(key_one) or scenarios.get("default") or {}
                sleep_s = scenario.get("sleep_s")
                if sleep_s:
                    time.sleep(float(sleep_s))
                out = scenario.get("stdout", "")
                err = scenario.get("stderr", "")
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                if err:
                    sys.stderr.write(err)
                    sys.stderr.flush()
                return int(scenario.get("exit_code", 0))

            if __name__ == "__main__":
                sys.exit(main())
            """
        ),
        encoding="utf-8",
    )

    if sys.platform.startswith("win"):
        executable = tmp_path / "fake_relay_cli.cmd"
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n', encoding="utf-8"
        )
    else:
        executable = tmp_path / "fake_relay_cli"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{py_path}" "$@"\n', encoding="utf-8"
        )
        executable.chmod(0o755)

    config_path = tmp_path / "scenarios.json"
    monkeypatch.setenv("FAKE_RELAY_CONFIG_FILE", str(config_path))
    # Config seam: run_bounded_relay_cli resolves its executable through
    # resolve_relay_cli_executable() (config -> env -> PATH) rather than taking
    # one as a parameter, so tests exercising it must inject via the SAME seam
    # a real deployment would use.
    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(executable))

    def set_scenarios(scenarios: dict[str, dict[str, Any]]) -> None:
        config_path.write_text(json.dumps(scenarios), encoding="utf-8")

    return str(executable), set_scenarios


def _wait_terminal(
    registry: RelayInstallJobRegistry, job_id: str, *, timeout_s: float = 5.0
) -> RelayInstallJob:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = registry.get(job_id)
        assert job is not None
        if job.terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


# --------------------------------------------------------------------------- #
# parse_relay_cli_stdout
# --------------------------------------------------------------------------- #


def test_parse_relay_cli_stdout_framed_marker_lines_in_order() -> None:
    """FAILING-FIRST: bootstrap's ``marker=json`` lines become ordered typed fields,
    unknown keys passed through verbatim (no allowlist)."""

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
    # Unknown key: value passed through verbatim, no JSON to decode.
    assert fields[2].value == "some literal text"
    assert fields[2].value_json is None


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
# effective_job_state
# --------------------------------------------------------------------------- #


def _job(*, state: str, last_output_at: str) -> RelayInstallJob:
    return RelayInstallJob(
        job_id="j1",
        kind="relay_cluster_bootstrap",
        argv=("cluster", "bootstrap"),
        created_at=last_output_at,
        updated_at=last_output_at,
        last_output_at=last_output_at,
        state=state,
    )


def test_effective_job_state_relabels_stale_running_job_needs_attention() -> None:
    """FAILING-FIRST: a RUNNING job with no output for longer than idle_seconds
    reports needs_user_attention -- derived fresh from wall-clock, no stored flag."""

    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    job = _job(state=STATE_RUNNING, last_output_at=stale)
    assert effective_job_state(job, idle_seconds=1.0) == STATE_NEEDS_USER_ATTENTION


def test_effective_job_state_fresh_running_job_stays_running() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    job = _job(state=STATE_RUNNING, last_output_at=now)
    assert effective_job_state(job, idle_seconds=60.0) == STATE_RUNNING


def test_effective_job_state_terminal_job_is_never_relabeled() -> None:
    """A COMPLETED/FAILED job's state is authoritative regardless of staleness."""

    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    job = _job(state=STATE_COMPLETED, last_output_at=stale)
    assert effective_job_state(job, idle_seconds=1.0) == STATE_COMPLETED


# --------------------------------------------------------------------------- #
# resolve_relay_cli_executable
# --------------------------------------------------------------------------- #


def test_resolve_relay_cli_executable_prefers_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST: CLIO_RELAY_CLI_PATH wins over a bare PATH lookup."""

    fake = tmp_path / "clio-relay-here"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(fake))
    assert resolve_relay_cli_executable() == str(fake)


def test_resolve_relay_cli_executable_unconfigured_and_absent_raises_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config override and nothing named clio-relay on PATH: typed refusal,
    never a bare FileNotFoundError from a downstream Popen."""

    import shutil

    monkeypatch.delenv("CLIO_RELAY_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RelayCliUnavailableError) as excinfo:
        resolve_relay_cli_executable()
    assert excinfo.value.reason == "relay_cli_unavailable"


# --------------------------------------------------------------------------- #
# run_bounded_relay_cli (register/status shape: awaited to completion)
# --------------------------------------------------------------------------- #


def test_run_bounded_relay_cli_happy_path(fake_relay_cli: tuple[str, ScenarioSetter]) -> None:
    executable, set_scenarios = fake_relay_cli
    set_scenarios({"cluster add": {"stdout": "", "exit_code": 0}})
    job = run_bounded_relay_cli(
        ["cluster", "add", "--name", "demo", "--ssh-host", "h"],
        kind="relay_cluster_register",
        timeout_seconds=10.0,
    )
    assert job.state == STATE_COMPLETED
    assert job.exit_code == 0
    assert job.terminal is True


def test_run_bounded_relay_cli_nonzero_exit_carries_bounded_stderr(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """FAILING-FIRST: a nonzero exit is a typed failure carrying the stderr tail,
    never a bare exception the caller has to introspect."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {"cluster add": {"stdout": "", "stderr": "error: cluster already exists", "exit_code": 1}}
    )
    job = run_bounded_relay_cli(
        ["cluster", "add", "--name", "demo", "--ssh-host", "h"],
        kind="relay_cluster_register",
        timeout_seconds=10.0,
    )
    assert job.state == STATE_FAILED
    assert job.exit_code == 1
    assert job.error_reason == "relay_cli_nonzero_exit"
    assert "cluster already exists" in job.stderr_tail


def test_run_bounded_relay_cli_timeout_is_typed(fake_relay_cli: tuple[str, ScenarioSetter]) -> None:
    """A wedged bounded call (register/status) times out typed, not a hang."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios({"cluster add": {"sleep_s": 5.0}})
    job = run_bounded_relay_cli(
        ["cluster", "add", "--name", "demo", "--ssh-host", "h"],
        kind="relay_cluster_register",
        timeout_seconds=0.3,
    )
    assert job.state == STATE_FAILED
    assert job.error_reason == "relay_cli_timeout"


def test_run_bounded_relay_cli_lingering_gate_actionable_refusal(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """FAILING-FIRST: the exit-78 lingering gate (surfaced by clio-relay as a
    generic nonzero exit + a known stderr signature) is classified into a typed,
    actionable refusal naming the enable-linger remediation -- never a bare
    relay_cli_nonzero_exit the caller has to pattern-match on itself."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {
            "relay-host install-proxy": {
                "stderr": (
                    "persistent frpc proxy requires systemd user lingering (Linger=yes)\n"
                    "run 'loginctl enable-linger relayuser' once, then retry\n"
                ),
                "exit_code": 1,
            }
        }
    )
    job = run_bounded_relay_cli(
        ["relay-host", "install-proxy", "--cluster", "demo"],
        kind="relay_proxy_install",
        timeout_seconds=10.0,
    )
    assert job.state == STATE_FAILED
    assert job.error_reason == "relay_proxy_lingering_required"
    assert job.actionable_refusal is not None
    assert job.actionable_refusal["reason"] == "relay_proxy_lingering_required"
    assert "enable-linger" in job.actionable_refusal["remediation"]


# --------------------------------------------------------------------------- #
# start_relay_install_job (bootstrap/session/proxy shape: handle-first + poll)
# --------------------------------------------------------------------------- #


def test_start_relay_install_job_returns_running_handle_then_reaches_terminal(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """FAILING-FIRST: the call returns a running handle immediately (never blocks
    on the SSH-dialing operation itself); the registry drives it to terminal on a
    background thread, folding receipt fields as they arrive, in order."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {
            "cluster bootstrap": {
                "stdout": (
                    'bootstrap_preflight_json={"ok": true}\n'
                    'bootstrap_target_identity_pinned={"trust": "first_use"}\n'
                    'bootstrap_receipt_json={"cluster": "demo", "installed": true}\n'
                ),
                "exit_code": 0,
            }
        }
    )
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=10.0,
    )
    # Handle-first: returned immediately, not yet necessarily terminal.
    assert job.kind == "relay_cluster_bootstrap"

    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_COMPLETED
    assert final.exit_code == 0
    assert [f.key for f in final.receipt_fields] == [
        "bootstrap_preflight_json",
        "bootstrap_target_identity_pinned",
        "bootstrap_receipt_json",
    ]
    assert [f.seq for f in final.receipt_fields] == [0, 1, 2]
    assert final.receipt_fields[1].value_json == {"trust": "first_use"}


def test_start_relay_install_job_timeout_kills_wedged_process(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """FAILING-FIRST: the runaway backstop actually reclaims a truly wedged
    subprocess (simulated SSH-prompt hang) instead of pinning the thread/registry
    slot forever."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios({"cluster bootstrap": {"sleep_s": 5.0}})
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=0.3,
    )
    final = _wait_terminal(registry, job.job_id, timeout_s=5.0)
    assert final.state == STATE_FAILED
    assert final.error_reason == "relay_cli_timeout"


def test_start_relay_install_job_spawn_failure_is_typed(tmp_path: Path) -> None:
    """A genuinely missing executable fails the job typed instead of raising out
    of the background thread unseen."""

    registry = RelayInstallJobRegistry()
    missing = tmp_path / "does-not-exist-relay-cli"
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=str(missing),
        timeout_seconds=10.0,
    )
    assert job.state == STATE_FAILED
    assert job.error_reason == "relay_cli_spawn_failed"
    assert job.terminal is True
