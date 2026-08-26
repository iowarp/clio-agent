"""clio-relay#209 A2: the curated tool surface (tools/relay_install_surface.py).

Exercises the five curated tools (register/bootstrap/status/session/proxy) through
:class:`RelayInstallSurface`, faking the relay CLI at the subprocess seam
(``CLIO_RELAY_CLI_PATH`` pointed at a locally-generated fake executable) -- no live
relay, no ssh. Covers: happy-path receipt parsing, nonzero-exit typed errors, the
exit-78 lingering-gate actionable refusal, argument validation, the F2 overwrite
guard, M1's asymmetric wheel/sha rule, M4's raise-to-envelope conversion, M8's
duplicate-run guard, and ``relay_cluster_status``'s three-way composition.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import clio_agent.tools.relay_install_surface as relay_install_surface_module
from clio_agent.tools.relay_cli_runner import STATE_COMPLETED, STATE_FAILED, STATE_HANDLE_ONLY
from clio_agent.tools.relay_install_surface import RelayInstallSurface

ScenarioSetter = Callable[[dict[str, dict[str, Any]]], None]


@pytest.fixture
def fake_relay_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScenarioSetter:
    """Point ``CLIO_RELAY_CLI_PATH`` at a fake executable; return a scenario setter.

    Scenarios are keyed on the invoked argv's two-token prefix (``"cluster
    bootstrap"``), falling back to the one-token prefix, then ``"default"`` -- so
    ``relay_cluster_status``'s three concurrent sub-probes (doctor/installation-info/
    relay-host proxy-status) can each be scripted independently in ONE test. The
    scenario file path travels via a FIXED location next to the fake script, never
    an env var (F5b proof: the real ``_subprocess_env()`` allowlist filters an
    unlisted env var exactly like it filters a real secret, which broke an
    env-var-based config channel here until this fix).
    """

    py_path = tmp_path / "fake_relay_cli.py"
    py_path.write_text(
        textwrap.dedent(
            """
            import json, os, sys, time
            from pathlib import Path

            def main() -> int:
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
                # child process received.
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
    monkeypatch.setenv("CLIO_RELAY_CLI_PATH", str(executable))
    # Keep long-op polling fast in tests without touching the timeout ceiling.
    monkeypatch.setenv("CLIO_RELAY_INSTALL_LONG_OP_TIMEOUT_S", "20")
    monkeypatch.setenv("CLIO_RELAY_INSTALL_BOUNDED_TIMEOUT_S", "10")

    def set_scenarios(scenarios: dict[str, dict[str, Any]]) -> None:
        config_path.write_text(json.dumps(scenarios), encoding="utf-8")

    return set_scenarios


@pytest.fixture
def surface() -> RelayInstallSurface:
    return RelayInstallSurface(cli_status={"configured": True, "reason": None})


async def _wait_terminal(
    surface: RelayInstallSurface,
    action_call: Callable[[], Any],
    job_id: str,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Poll ``action_call`` (a ``status`` invocation) until the job is terminal."""

    deadline = time.monotonic() + timeout_s
    result: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        result = await action_call()
        if result["terminal"]:
            return result
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


# --------------------------------------------------------------------------- #
# relay_cluster_register (bounded) -- F2 overwrite guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cluster_register_new_cluster_happy_path(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """A cluster NOT already in `cluster list` registers without needing replace=true."""

    fake_relay_cli(
        {
            "cluster list": {"stdout": "other-cluster ssh=x profile=linux-user\n", "exit_code": 0},
            "cluster add": {"stdout": "", "exit_code": 0},
        }
    )
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_COMPLETED
    assert result["kind"] == "relay_cluster_register"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_cluster_register_existing_cluster_refuses_without_replace(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST (F2, the #1244-class bug): re-registering an ALREADY
    registered cluster without replace=true is a typed refusal -- never a silent
    full-replace wiping target_identity/frp_transport/worker capacity. Returned
    as an envelope (M4), never raised."""

    fake_relay_cli(
        {
            "cluster list": {
                "stdout": "demo ssh=demo.example.org profile=linux-user worker_concurrency=3 control_query_concurrency=1\n",
                "exit_code": 0,
            },
            # If the refusal did NOT fire, this scenario would make the (buggy)
            # overwrite look identical to success -- proving the assertion below
            # is really catching the refusal, not an absent 'cluster add' call.
            "cluster add": {"stdout": "", "exit_code": 0},
        }
    )
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_cluster_already_registered"
    assert result["terminal"] is True


@pytest.mark.asyncio
async def test_cluster_register_existing_cluster_replace_true_overwrites(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """A confirmed replace=true proceeds with the overwrite."""

    fake_relay_cli(
        {
            "cluster list": {
                "stdout": "demo ssh=demo.example.org profile=linux-user\n",
                "exit_code": 0,
            },
            "cluster add": {"stdout": "", "exit_code": 0},
        }
    )
    result = await surface.invoke(
        "relay_cluster_register",
        {"cluster": "demo", "ssh_host": "demo.example.org", "replace": True},
    )
    assert result["state"] == STATE_COMPLETED


@pytest.mark.asyncio
async def test_cluster_register_similar_name_is_not_a_false_positive_match(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """'demo2' being registered must not false-positive-match a query for 'demo'."""

    fake_relay_cli(
        {
            "cluster list": {
                "stdout": "demo2 ssh=demo2.example.org profile=linux-user\n",
                "exit_code": 0,
            },
            "cluster add": {"stdout": "", "exit_code": 0},
        }
    )
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_COMPLETED


@pytest.mark.asyncio
async def test_cluster_register_dev_mode_argument_removed(surface: RelayInstallSurface) -> None:
    """FAILING-FIRST (F2c): dev_mode is not merely unused -- it is REJECTED as an
    unknown argument (additionalProperties: false), so an agent cannot pass it to
    downgrade the verification chain even by accident."""

    tool = await surface.server.get_tool("cluster_register")
    assert "dev_mode" not in tool.parameters.get("properties", {})
    assert tool.parameters.get("additionalProperties") is False


@pytest.mark.asyncio
async def test_cluster_register_nonzero_exit_typed(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """A rejected registration surfaces as a typed failure carrying the bounded
    stderr tail, not a bare exception."""

    fake_relay_cli(
        {
            "cluster list": {"stdout": "", "exit_code": 0},
            "cluster add": {"stderr": "error: invalid ssh host", "exit_code": 1},
        }
    )
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_cli_nonzero_exit"
    assert "invalid ssh host" in result["stderr_tail"]


# --------------------------------------------------------------------------- #
# relay_cluster_bootstrap (long op: start + status poll)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cluster_bootstrap_start_then_status_parses_framed_receipt(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    fake_relay_cli(
        {
            "cluster bootstrap": {
                "stdout": (
                    'bootstrap_preflight_json={"ok": true}\n'
                    'bootstrap_target_identity_pinned={"trust": "first_use", "hostnames": ["h1"]}\n'
                    'bootstrap_receipt_json={"cluster": "demo", "installed": true}\n'
                ),
                "exit_code": 0,
            }
        }
    )
    started = await surface.invoke(
        "relay_cluster_bootstrap", {"action": "start", "cluster": "demo"}
    )
    assert started["terminal"] is False
    assert started["kind"] == "relay_cluster_bootstrap"
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke(
            "relay_cluster_bootstrap", {"action": "status", "job_id": job_id}
        )

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_COMPLETED
    keys = [f["key"] for f in final["receipt_fields"]]
    assert keys == [
        "bootstrap_preflight_json",
        "bootstrap_target_identity_pinned",
        "bootstrap_receipt_json",
    ]
    assert [f["seq"] for f in final["receipt_fields"]] == [0, 1, 2]
    pinned = final["receipt_fields"][1]
    assert pinned["value_json"]["trust"] == "first_use"


@pytest.mark.asyncio
async def test_cluster_bootstrap_surfaces_unrecognized_marker_count_and_document_truncation_flag(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """R3/R4 wire-shape proof: unrecognized_marker_count and
    parsed_document_truncated reach the curated tool's wire output (through
    RelayInstallJob.to_wire()/_render), not just the underlying dataclass."""

    fake_relay_cli(
        {
            "cluster bootstrap": {
                "stdout": (
                    'bootstrap_preflight_json={"ok": true}\n'
                    "SOME_FUTURE_NAMESPACE_marker=unrecognized\n"
                ),
                "exit_code": 0,
            }
        }
    )
    started = await surface.invoke(
        "relay_cluster_bootstrap", {"action": "start", "cluster": "demo"}
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke(
            "relay_cluster_bootstrap", {"action": "status", "job_id": job_id}
        )

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_COMPLETED
    assert final["unrecognized_marker_count"] == 1
    assert final["parsed_document_truncated"] is False


@pytest.mark.asyncio
async def test_cluster_bootstrap_wheel_without_sha_is_refused(
    surface: RelayInstallSurface,
) -> None:
    """A wheel REQUIRES its sha (M1) -- an unverified custom wheel is refused."""

    result = await surface.invoke(
        "relay_cluster_bootstrap",
        {"action": "start", "cluster": "demo", "relay_wheel": "/x/relay.whl"},
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_cluster_bootstrap_sha_only_is_accepted(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST (M1): clio-relay's real rule is asymmetric -- a sha ALONE
    (pinning a resolved wheel's verification) is a legitimate, documented
    release-pinning path and must NOT be blocked the way the prior symmetric
    wheel<->sha XOR check did."""

    fake_relay_cli({"cluster bootstrap": {"stdout": "", "exit_code": 0}})
    started = await surface.invoke(
        "relay_cluster_bootstrap",
        {"action": "start", "cluster": "demo", "relay_artifact_sha256": "abc123"},
    )
    assert started["terminal"] is False
    assert started["error_reason"] == ""


@pytest.mark.asyncio
async def test_cluster_bootstrap_status_unknown_job_is_typed_envelope(
    surface: RelayInstallSurface,
) -> None:
    """M4: an unknown job id returns a terminal envelope, never a raised exception."""

    result = await surface.invoke("relay_cluster_bootstrap", {"action": "status", "job_id": "nope"})
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_install_job_not_found"
    assert result["terminal"] is True


@pytest.mark.asyncio
async def test_cluster_bootstrap_duplicate_run_refused_naming_live_job_id(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST (M8): a second bootstrap for the SAME cluster while one is
    still running is refused, naming the already-live job_id."""

    fake_relay_cli({"cluster bootstrap": {"sleep_s": 2.0, "exit_code": 0}})
    first = await surface.invoke("relay_cluster_bootstrap", {"action": "start", "cluster": "demo"})
    assert first["terminal"] is False

    second = await surface.invoke("relay_cluster_bootstrap", {"action": "start", "cluster": "demo"})
    assert second["state"] == STATE_FAILED
    assert second["error_reason"] == "relay_install_job_already_running"
    assert second["job_id"] == first["job_id"]

    # A DIFFERENT cluster is unaffected by the guard.
    other = await surface.invoke("relay_cluster_bootstrap", {"action": "start", "cluster": "other"})
    assert other["terminal"] is False
    assert other["job_id"] != first["job_id"]


# --------------------------------------------------------------------------- #
# relay_cluster_status (bounded 3-way composition)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cluster_status_composes_three_subprobes_independently(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """One failing sub-probe (proxy-status) never masks or fails the other two
    (doctor, installation-info) -- each surfaces its own typed status."""

    fake_relay_cli(
        {
            "doctor": {"stdout": "cluster demo: reachable\n", "exit_code": 0},
            "installation-info": {"stdout": '{"version": "1.5.15"}\n', "exit_code": 0},
            "relay-host proxy-status": {"stderr": "proxy not installed", "exit_code": 1},
        }
    )
    result = await surface.invoke("relay_cluster_status", {"cluster": "demo"})
    assert result["cluster"] == "demo"
    assert result["doctor"]["state"] == STATE_COMPLETED
    assert result["installation_info"]["state"] == STATE_COMPLETED
    assert result["installation_info"]["parsed_document"] == {"version": "1.5.15"}
    assert result["proxy_status"]["state"] == STATE_FAILED
    assert result["proxy_status"]["error_reason"] == "relay_cli_nonzero_exit"


@pytest.mark.asyncio
async def test_cluster_status_missing_cluster_is_typed_envelope(
    surface: RelayInstallSurface,
) -> None:
    """M4: the composed-document shape is preserved even for a refusal -- all
    three sub-fields carry the SAME typed reason, never a raised exception."""

    result = await surface.invoke("relay_cluster_status", {})
    assert result["doctor"]["error_reason"] == "relay_install_arguments_invalid"
    assert result["installation_info"]["error_reason"] == "relay_install_arguments_invalid"
    assert result["proxy_status"]["error_reason"] == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_cluster_status_does_not_convert_cancellation_to_a_probe_failure(
    surface: RelayInstallSurface,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation remains control flow instead of becoming a typed CLI refusal."""

    def cancel_probe(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(relay_install_surface_module, "run_bounded_relay_cli", cancel_probe)

    with pytest.raises(asyncio.CancelledError):
        await surface.cluster_status({"cluster": "demo"})


# --------------------------------------------------------------------------- #
# relay_session_lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_session_lifecycle_start_then_status(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    fake_relay_cli({"session start": {"stdout": '{"state": "ready", "session_id": "s1"}\n'}})
    started = await surface.invoke(
        "relay_session_lifecycle",
        {"action": "start", "cluster": "demo", "session_id": "s1"},
    )
    assert started["kind"] == "relay_session_start"
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke(
            "relay_session_lifecycle", {"action": "status", "job_id": job_id}
        )

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_COMPLETED
    assert final["parsed_document"] == {"state": "ready", "session_id": "s1"}


@pytest.mark.asyncio
async def test_session_lifecycle_start_exit_2_is_handle_only(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """M2, through the curated tool: a durable-but-not-yet-usable start settles
    handle_only, never failed."""

    fake_relay_cli(
        {"session start": {"stdout": '{"state": "starting", "usable": false}\n', "exit_code": 2}}
    )
    started = await surface.invoke(
        "relay_session_lifecycle",
        {"action": "start", "cluster": "demo", "session_id": "s1"},
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke(
            "relay_session_lifecycle", {"action": "status", "job_id": job_id}
        )

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_HANDLE_ONLY
    assert final["error_reason"] == ""


@pytest.mark.asyncio
async def test_session_lifecycle_start_exit_2_without_document_is_failed(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST (R2, CRITICAL), through the curated tool: click's own
    UsageError also exits 2 -- a bad argument to 'session start' (empty
    stdout, exit 2) must settle FAILED with a typed reason through the whole
    curated-tool path, never handle_only with an empty error_reason (a failed
    start reported as a durable handle)."""

    fake_relay_cli(
        {
            "session start": {
                "stdout": "",
                "stderr": "Error: Missing option '--session-id'.\n",
                "exit_code": 2,
            }
        }
    )
    started = await surface.invoke(
        "relay_session_lifecycle",
        {"action": "start", "cluster": "demo", "session_id": "s1"},
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke(
            "relay_session_lifecycle", {"action": "status", "job_id": job_id}
        )

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_FAILED
    assert final["error_reason"] == "relay_session_start_exit2_undocumented"
    assert final["parsed_document"] is None


@pytest.mark.asyncio
async def test_session_lifecycle_teardown_scheduler_cancel_requires_cancel_jobs(
    surface: RelayInstallSurface,
) -> None:
    """cancel_scheduler_jobs without cancel_jobs is a typed validation refusal
    (mirrors clio-relay's own CLI precondition)."""

    result = await surface.invoke(
        "relay_session_lifecycle",
        {
            "action": "teardown",
            "cluster": "demo",
            "session_id": "s1",
            "cancel_scheduler_jobs": True,
        },
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_session_lifecycle_missing_cluster_is_typed_envelope(
    surface: RelayInstallSurface,
) -> None:
    result = await surface.invoke("relay_session_lifecycle", {"action": "attach"})
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_install_arguments_invalid"


# --------------------------------------------------------------------------- #
# relay_proxy_lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proxy_lifecycle_install_lingering_gate_actionable_refusal(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """exit-78's lingering gate (surfaced by clio-relay as a bare nonzero exit +
    a known stderr signature) becomes a typed actionable_refusal naming the
    enable-linger remediation on the terminal job result."""

    fake_relay_cli(
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
    started = await surface.invoke(
        "relay_proxy_lifecycle", {"action": "install_proxy", "cluster": "demo"}
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke("relay_proxy_lifecycle", {"action": "status", "job_id": job_id})

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_FAILED
    assert final["error_reason"] == "relay_proxy_lingering_required"
    assert final["actionable_refusal"]["reason"] == "relay_proxy_lingering_required"
    assert "enable-linger" in final["actionable_refusal"]["remediation"]


@pytest.mark.asyncio
async def test_proxy_lifecycle_teardown_happy_path(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    fake_relay_cli({"relay-host teardown-proxy": {"stdout": '{"removed": true}\n'}})
    started = await surface.invoke(
        "relay_proxy_lifecycle", {"action": "teardown_proxy", "cluster": "demo"}
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke("relay_proxy_lifecycle", {"action": "status", "job_id": job_id})

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_COMPLETED
    assert final["parsed_document"] == {"removed": True}


@pytest.mark.asyncio
async def test_proxy_lifecycle_custom_frp_token_env_reaches_the_real_child(
    surface: RelayInstallSurface,
    fake_relay_cli: ScenarioSetter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILING-FIRST (R1's last bullet), through the curated tool: a cluster
    whose frp_transport.token_env is non-default is not resolvable by this
    surface on its own (the cluster definition is not available at this
    layer), so the CALLER names the env var explicitly via frp_token_env --
    proven reaching the real child process, not just an intermediate dict."""

    monkeypatch.setenv("MY_CLUSTER_FRP_TOKEN", "custom-secret-value")
    fake_relay_cli(
        {"relay-host install-proxy": {"echo_env": ["MY_CLUSTER_FRP_TOKEN"], "exit_code": 0}}
    )
    started = await surface.invoke(
        "relay_proxy_lifecycle",
        {"action": "install_proxy", "cluster": "demo", "frp_token_env": "MY_CLUSTER_FRP_TOKEN"},
    )
    job_id = started["job_id"]

    async def poll() -> dict[str, Any]:
        return await surface.invoke("relay_proxy_lifecycle", {"action": "status", "job_id": job_id})

    final = await _wait_terminal(surface, poll, job_id)
    assert final["state"] == STATE_COMPLETED
    assert "ENV:MY_CLUSTER_FRP_TOKEN=custom-secret-value" in final["stdout_tail"]


@pytest.mark.asyncio
async def test_proxy_lifecycle_missing_cluster_is_typed_envelope(
    surface: RelayInstallSurface,
) -> None:
    result = await surface.invoke("relay_proxy_lifecycle", {"action": "install_proxy"})
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_proxy_lifecycle_cli_unavailable_is_typed_envelope(
    surface: RelayInstallSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4: relay_cli_unavailable is also an envelope, never a raised exception."""

    monkeypatch.delenv("CLIO_RELAY_CLI_PATH", raising=False)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result = await surface.invoke(
        "relay_proxy_lifecycle", {"action": "install_proxy", "cluster": "demo"}
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_cli_unavailable"


# --------------------------------------------------------------------------- #
# tool mounting shape + annotations (F2b)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_surface_mounts_exactly_five_curated_tools(surface: RelayInstallSurface) -> None:
    tools = await surface.server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "cluster_bootstrap",
        "cluster_register",
        "cluster_status",
        "proxy_lifecycle",
        "session_lifecycle",
    ]


@pytest.mark.asyncio
async def test_register_tool_declares_destructive_and_non_idempotent(
    surface: RelayInstallSurface,
) -> None:
    """FAILING-FIRST (F2b): register can now perform a full destructive replace,
    so its annotations must say so -- the #1244-class bug was partly that
    idempotent_hint=True/destructive_hint=False described it as harmless."""

    tool = await surface.server.get_tool("cluster_register")
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is False
