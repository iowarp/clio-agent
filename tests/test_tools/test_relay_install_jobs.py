"""clio-relay#209 A2: job execution + registry (tools/relay_install_jobs.py).

Subprocess-level tests exercising :func:`run_bounded_relay_cli` and
:func:`start_relay_install_job` directly (bypassing the curated tool surface)
against a locally-generated FAKE ``clio-relay`` executable -- no live relay, no ssh,
no mocked ``subprocess`` internals -- plus pure-unit coverage of
:class:`RelayInstallJobRegistry`'s retention (F4) and receipt-field capping (F3).
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from clio_agent.tools import relay_cli_runner, relay_install_jobs
from clio_agent.tools.relay_cli_runner import (
    REASON_SESSION_START_EXIT2_UNDOCUMENTED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_HANDLE_ONLY,
    STATE_NEEDS_USER_ATTENTION,
    STATE_RUNNING,
    RelayCliReceiptField,
)
from clio_agent.tools.relay_install_jobs import (
    RelayInstallJob,
    RelayInstallJobRegistry,
    default_relay_install_job_registry,
    effective_job_state,
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
            from pathlib import Path

            def main() -> int:
                # F5b proof: the scenario file path travels via a FIXED location next
                # to this script, never an env var -- the real _subprocess_env()
                # allowlist would otherwise filter out a test-only env var exactly
                # like it filters a real secret, breaking this fixture's own config
                # channel (caught live: every scenario silently fell back to the
                # empty default once the allowlist landed).
                config_path = Path(__file__).resolve().parent / "scenarios.json"
                scenarios = {}
                if config_path.exists():
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
                # R1 end-to-end proof: echo back exactly which env vars the REAL
                # child process received (never an assertion about intent).
                for name in scenario.get("echo_env", []):
                    sys.stdout.write(f"ENV:{name}={os.environ.get(name, '<unset>')}\\n")
                if out or scenario.get("echo_env"):
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
# effective_job_state
# --------------------------------------------------------------------------- #


def _job(*, state: str, last_output_at: str, cluster: str = "demo") -> RelayInstallJob:
    return RelayInstallJob(
        job_id="j1",
        kind="relay_cluster_bootstrap",
        argv=("cluster", "bootstrap"),
        created_at=last_output_at,
        updated_at=last_output_at,
        last_output_at=last_output_at,
        cluster=cluster,
        state=state,
    )


def test_effective_job_state_relabels_stale_running_job_needs_attention() -> None:
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
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    job = _job(state=STATE_COMPLETED, last_output_at=stale)
    assert effective_job_state(job, idle_seconds=1.0) == STATE_COMPLETED


# --------------------------------------------------------------------------- #
# run_bounded_relay_cli (register/status shape: awaited to completion)
# --------------------------------------------------------------------------- #


def test_run_bounded_relay_cli_happy_path(fake_relay_cli: tuple[str, ScenarioSetter]) -> None:
    _, set_scenarios = fake_relay_cli
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
    _, set_scenarios = fake_relay_cli
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
    _, set_scenarios = fake_relay_cli
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
    _, set_scenarios = fake_relay_cli
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


def test_run_bounded_relay_cli_tail_bytes_override(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """F2 correctness seam: a caller can request a LARGER retained tail than the
    configured default (used by the cluster-register existence check)."""

    _, set_scenarios = fake_relay_cli
    big_line = "x" * 200
    set_scenarios({"cluster list": {"stdout": big_line, "exit_code": 0}})
    job = run_bounded_relay_cli(
        ["cluster", "list"], kind="relay_cluster_list", timeout_seconds=10.0, tail_bytes=50
    )
    assert len(job.stdout_tail.encode("utf-8")) <= 50
    job_full = run_bounded_relay_cli(
        ["cluster", "list"], kind="relay_cluster_list", timeout_seconds=10.0, tail_bytes=1 << 16
    )
    assert job_full.stdout_tail == big_line


# --------------------------------------------------------------------------- #
# start_relay_install_job (bootstrap/session/proxy shape: handle-first + poll)
# --------------------------------------------------------------------------- #


def test_start_relay_install_job_returns_running_handle_then_reaches_terminal(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """The call returns a running handle immediately (never blocks on the
    SSH-dialing operation itself); the registry drives it to terminal on a
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
        cluster="demo",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=10.0,
    )
    assert job.kind == "relay_cluster_bootstrap"
    assert job.cluster == "demo"

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
    assert final.receipt_fields_truncated is False


def test_start_relay_install_job_session_start_exit_2_is_handle_only(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """M2, end to end: a real subprocess exiting 2 for relay_session_start settles
    handle_only, not failed, with error_reason left empty."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {"session start": {"stdout": '{"state": "starting", "usable": false}\n', "exit_code": 2}}
    )
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_session_start",
        cluster="demo",
        argv=["session", "start", "--cluster", "demo", "--session-id", "s1"],
        executable=executable,
        timeout_seconds=10.0,
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_HANDLE_ONLY
    assert final.error_reason == ""
    assert final.parsed_document == {"state": "starting", "usable": False}


def test_start_relay_install_job_session_start_exit_2_without_document_is_failed(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """FAILING-FIRST (R2, CRITICAL), end to end: click's own UsageError also
    exits 2 -- a bad argument to 'session start' (empty stdout, a usage message
    on stderr, exit 2) must settle FAILED with a typed reason, never
    handle_only with an empty error_reason (which would report a failed start
    as a durable success)."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {
            "session start": {
                "stdout": "",
                "stderr": "Error: Missing option '--session-id'.\n",
                "exit_code": 2,
            }
        }
    )
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_session_start",
        cluster="demo",
        argv=["session", "start", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=10.0,
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_FAILED
    assert final.error_reason == REASON_SESSION_START_EXIT2_UNDOCUMENTED
    assert final.parsed_document is None
    assert "Missing option" in final.stderr_tail


def test_start_relay_install_job_parses_document_larger_than_display_tail(
    fake_relay_cli: tuple[str, ScenarioSetter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (R3, MUST FIX): a whole JSON document bigger than the SMALL
    display tail (output_tail_bytes, forced down to 100 here) must still be
    parsed into parsed_document -- proving the async driver parses off its own
    separate, larger accumulator, never off the already-clipped display tail."""

    monkeypatch.setattr(relay_cli_runner, "output_tail_bytes", lambda: 100)
    monkeypatch.setattr(relay_install_jobs, "output_tail_bytes", lambda: 100)
    big_payload = "x" * 5000
    document = json.dumps({"state": "ready", "session_id": "s1", "padding": big_payload})
    assert len(document.encode("utf-8")) > 100  # bigger than the forced-down display tail
    executable, set_scenarios = fake_relay_cli
    set_scenarios({"session start": {"stdout": document + "\n", "exit_code": 0}})
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_session_start",
        cluster="demo",
        argv=["session", "start", "--cluster", "demo", "--session-id", "s1"],
        executable=executable,
        timeout_seconds=10.0,
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_COMPLETED
    assert final.parsed_document is not None
    assert final.parsed_document["session_id"] == "s1"
    assert final.parsed_document["padding"] == big_payload
    assert final.parsed_document_truncated is False
    # The display tail stays bounded regardless -- this is NOT what fed the parse.
    assert len(final.stdout_tail.encode("utf-8")) <= 100


def test_start_relay_install_job_document_past_the_parse_bound_is_typed_truncated(
    fake_relay_cli: tuple[str, ScenarioSetter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST (R3, MUST FIX): a document that ALSO exceeds the larger
    parse-bound must never be silently dropped -- parsed_document stays None
    AND parsed_document_truncated is set, a typed, queryable signal instead of
    the loss being invisible."""

    monkeypatch.setattr(relay_cli_runner, "parsed_document_max_bytes", lambda: 200)
    monkeypatch.setattr(relay_install_jobs, "parsed_document_max_bytes", lambda: 200)
    document = json.dumps({"state": "ready", "padding": "x" * 5000})
    executable, set_scenarios = fake_relay_cli
    set_scenarios({"session start": {"stdout": document + "\n", "exit_code": 0}})
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_session_start",
        cluster="demo",
        argv=["session", "start", "--cluster", "demo", "--session-id", "s1"],
        executable=executable,
        timeout_seconds=10.0,
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_COMPLETED
    assert final.parsed_document is None
    assert final.parsed_document_truncated is True


def test_start_relay_install_job_surfaces_unrecognized_marker_count(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    """R4 (small fix): a stdout line matching the key=value framed shape but
    NOT under a declared marker namespace is still typed-counted (never
    promoted to a field -- F5a stays intact), so schema drift against a newer
    clio-relay release is queryable instead of silently invisible."""

    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {
            "cluster bootstrap": {
                "stdout": (
                    'bootstrap_preflight_json={"ok": true}\n'
                    "SOME_FUTURE_NAMESPACE_marker=unrecognized\n"
                    "ANOTHER_UNKNOWN=value\n"
                ),
                "exit_code": 0,
            }
        }
    )
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        cluster="demo",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=10.0,
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_COMPLETED
    assert [f.key for f in final.receipt_fields] == ["bootstrap_preflight_json"]
    assert final.unrecognized_marker_count == 2


def test_start_relay_install_job_extra_env_names_reach_the_real_child(
    fake_relay_cli: tuple[str, ScenarioSetter], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1's last bullet, end to end: a caller-supplied extra_env_names name
    (standing in for a cluster's non-default frp_transport.token_env) actually
    reaches the real spawned subprocess -- proven on the REAL child environment
    (an echoed ``ENV:...`` line), not just on the dict ``_subprocess_env``
    builds in isolation."""

    monkeypatch.setenv("MY_CUSTOM_FRP_TOKEN_ENV", "custom-secret-value")
    executable, set_scenarios = fake_relay_cli
    set_scenarios(
        {
            "relay-host": {
                "echo_env": ["MY_CUSTOM_FRP_TOKEN_ENV"],
                "exit_code": 0,
            }
        }
    )
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_proxy_install",
        cluster="demo",
        argv=["relay-host", "install-proxy", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=10.0,
        extra_env_names=("MY_CUSTOM_FRP_TOKEN_ENV",),
    )
    final = _wait_terminal(registry, job.job_id)
    assert final.state == STATE_COMPLETED
    assert "ENV:MY_CUSTOM_FRP_TOKEN_ENV=custom-secret-value" in final.stdout_tail


def test_default_relay_install_job_registry_is_a_process_wide_singleton() -> None:
    """M7's core mechanism: repeated calls return the SAME registry object --
    this is what lets production surface reconstruction thread one shared
    ledger through every TTL-triggered rebuild instead of each one minting a
    fresh, empty registry."""

    first = default_relay_install_job_registry()
    second = default_relay_install_job_registry()
    assert first is second


def test_start_relay_install_job_timeout_kills_wedged_process(
    fake_relay_cli: tuple[str, ScenarioSetter],
) -> None:
    executable, set_scenarios = fake_relay_cli
    set_scenarios({"cluster bootstrap": {"sleep_s": 5.0}})
    registry = RelayInstallJobRegistry()
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        cluster="demo",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=executable,
        timeout_seconds=0.3,
    )
    final = _wait_terminal(registry, job.job_id, timeout_s=5.0)
    assert final.state == STATE_FAILED
    assert final.error_reason == "relay_cli_timeout"


def test_start_relay_install_job_spawn_failure_is_typed(tmp_path: Path) -> None:
    registry = RelayInstallJobRegistry()
    missing = tmp_path / "does-not-exist-relay-cli"
    job = start_relay_install_job(
        registry,
        kind="relay_cluster_bootstrap",
        cluster="demo",
        argv=["cluster", "bootstrap", "--cluster", "demo"],
        executable=str(missing),
        timeout_seconds=10.0,
    )
    assert job.state == STATE_FAILED
    assert job.error_reason == "relay_cli_spawn_failed"
    assert job.terminal is True


# --------------------------------------------------------------------------- #
# RelayInstallJobRegistry: receipt-field capping (F3) + retention (F4) + find_running (M8)
# --------------------------------------------------------------------------- #


def test_append_receipt_field_caps_and_marks_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST (F3): past the retained-field cap, further markers are
    dropped from the list (never silently -- receipt_fields_truncated is set)."""

    monkeypatch.setattr(relay_install_jobs, "MAX_RETAINED_RECEIPT_FIELDS", 3)
    registry = RelayInstallJobRegistry()
    now = "2026-01-01T00:00:00+00:00"
    job = RelayInstallJob(
        job_id="j1",
        kind="relay_cluster_bootstrap",
        argv=(),
        created_at=now,
        updated_at=now,
        last_output_at=now,
    )
    registry.register(job)
    for i in range(5):
        registry.append_receipt_field(
            "j1", RelayCliReceiptField(seq=0, key=f"bootstrap_field_{i}", value=str(i))
        )
    final = registry.get("j1")
    assert final is not None
    assert len(final.receipt_fields) == 3
    assert [f.key for f in final.receipt_fields] == [
        "bootstrap_field_0",
        "bootstrap_field_1",
        "bootstrap_field_2",
    ]
    assert final.receipt_fields_truncated is True


def test_field_count_is_o1_and_never_materializes_snapshot() -> None:
    registry = RelayInstallJobRegistry()
    now = "2026-01-01T00:00:00+00:00"
    job = RelayInstallJob(
        job_id="j1", kind="k", argv=(), created_at=now, updated_at=now, last_output_at=now
    )
    registry.register(job)
    assert registry.field_count("j1") == 0
    registry.append_receipt_field("j1", RelayCliReceiptField(seq=0, key="bootstrap_x", value="1"))
    assert registry.field_count("j1") == 1


def test_registry_retention_evicts_oldest_terminal_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST (F4): past the soft cap, the OLDEST TERMINAL job is evicted
    first; a still-running job is preserved."""

    monkeypatch.setattr(relay_install_jobs, "job_retention_max_entries", lambda: 2)
    monkeypatch.setattr(relay_install_jobs, "job_retention_hard_cap", lambda: 10)
    registry = RelayInstallJobRegistry()
    now = "2026-01-01T00:00:00+00:00"

    def _mk(job_id: str) -> RelayInstallJob:
        return RelayInstallJob(
            job_id=job_id, kind="k", argv=(), created_at=now, updated_at=now, last_output_at=now
        )

    registry.register(_mk("old-terminal"))
    registry.set_terminal("old-terminal", state=STATE_COMPLETED, exit_code=0)
    registry.register(_mk("still-running"))
    # Registering a third job pushes the registry past max_entries=2; the oldest
    # TERMINAL job ("old-terminal") is evicted, the running one is preserved.
    registry.register(_mk("newcomer"))

    assert registry.get("old-terminal") is None
    assert registry.get("still-running") is not None
    assert registry.get("newcomer") is not None


def test_registry_retention_force_evicts_past_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAILING-FIRST (F4): with no terminal jobs available, the hard cap still
    forces the oldest job out (a bounded registry accepted tradeoff -- its
    subprocess, if any, is simply no longer pollable)."""

    monkeypatch.setattr(relay_install_jobs, "job_retention_max_entries", lambda: 1)
    monkeypatch.setattr(relay_install_jobs, "job_retention_hard_cap", lambda: 2)
    registry = RelayInstallJobRegistry()
    now = "2026-01-01T00:00:00+00:00"

    def _mk(job_id: str) -> RelayInstallJob:
        return RelayInstallJob(
            job_id=job_id, kind="k", argv=(), created_at=now, updated_at=now, last_output_at=now
        )

    registry.register(_mk("first"))
    registry.register(_mk("second"))
    registry.register(_mk("third"))

    assert registry.get("first") is None
    assert registry.get("second") is not None
    assert registry.get("third") is not None


def test_find_running_matches_cluster_and_kind_only_when_non_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M8's lookup primitive: a terminal job for the same (cluster, kind) is not a
    match; a running job for a DIFFERENT cluster or kind is not a match either."""

    monkeypatch.setattr(relay_install_jobs, "job_retention_max_entries", lambda: 200)
    monkeypatch.setattr(relay_install_jobs, "job_retention_hard_cap", lambda: 400)
    registry = RelayInstallJobRegistry()
    now = "2026-01-01T00:00:00+00:00"
    running = RelayInstallJob(
        job_id="running-job",
        kind="relay_cluster_bootstrap",
        argv=(),
        created_at=now,
        updated_at=now,
        last_output_at=now,
        cluster="demo",
    )
    other_cluster = RelayInstallJob(
        job_id="other-cluster-job",
        kind="relay_cluster_bootstrap",
        argv=(),
        created_at=now,
        updated_at=now,
        last_output_at=now,
        cluster="other",
    )
    registry.register(running)
    registry.register(other_cluster)
    registry.set_terminal("other-cluster-job", state=STATE_COMPLETED, exit_code=0)

    found = registry.find_running(cluster="demo", kind="relay_cluster_bootstrap")
    assert found is not None
    assert found.job_id == "running-job"

    assert registry.find_running(cluster="other", kind="relay_cluster_bootstrap") is None
    assert registry.find_running(cluster="demo", kind="relay_proxy_install") is None
