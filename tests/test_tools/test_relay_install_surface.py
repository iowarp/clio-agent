"""clio-relay#209 A2: the curated tool surface (tools/relay_install_surface.py).

Exercises the five curated tools (register/bootstrap/status/session/proxy) through
:class:`RelayInstallSurface`, faking the relay CLI at the subprocess seam
(``CLIO_RELAY_CLI_PATH`` pointed at a locally-generated fake executable) -- no live
relay, no ssh. Covers: happy-path receipt parsing, nonzero-exit typed errors, the
exit-78 lingering-gate actionable refusal, argument validation, and
``relay_cluster_status``'s three-way composition.
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
    RelayCliJobError,
)
from clio_agent.tools.relay_install_surface import RelayInstallSurface

ScenarioSetter = Callable[[dict[str, dict[str, Any]]], None]


@pytest.fixture
def fake_relay_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScenarioSetter:
    """Point ``CLIO_RELAY_CLI_PATH`` at a fake executable; return a scenario setter.

    Scenarios are keyed on the invoked argv's two-token prefix (``"cluster
    bootstrap"``), falling back to the one-token prefix, then ``"default"`` -- so
    ``relay_cluster_status``'s three concurrent sub-probes (doctor/installation-info/
    relay-host proxy-status) can each be scripted independently in ONE test.
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
# relay_cluster_register (bounded)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cluster_register_happy_path(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    fake_relay_cli({"cluster add": {"stdout": "", "exit_code": 0}})
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_COMPLETED
    assert result["kind"] == "relay_cluster_register"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_cluster_register_nonzero_exit_typed(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST: a rejected registration surfaces as a typed failure carrying
    the bounded stderr tail, not a bare exception."""

    fake_relay_cli(
        {"cluster add": {"stderr": "error: cluster 'demo' already registered", "exit_code": 1}}
    )
    result = await surface.invoke(
        "relay_cluster_register", {"cluster": "demo", "ssh_host": "demo.example.org"}
    )
    assert result["state"] == STATE_FAILED
    assert result["error_reason"] == "relay_cli_nonzero_exit"
    assert "already registered" in result["stderr_tail"]


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
    # order preserved (seq matches list position -- "published in order")
    assert [f["seq"] for f in final["receipt_fields"]] == [0, 1, 2]
    pinned = final["receipt_fields"][1]
    assert pinned["value_json"]["trust"] == "first_use"


@pytest.mark.asyncio
async def test_cluster_bootstrap_requires_wheel_and_sha_together(
    surface: RelayInstallSurface,
) -> None:
    """FAILING-FIRST: a half-specified relay_wheel/relay_artifact_sha256 pair is a
    typed validation refusal raised BEFORE any subprocess is spawned."""

    with pytest.raises(RelayCliJobError) as excinfo:
        await surface.invoke(
            "relay_cluster_bootstrap",
            {"action": "start", "cluster": "demo", "relay_wheel": "/x/relay.whl"},
        )
    assert excinfo.value.reason == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_cluster_bootstrap_status_unknown_job_is_typed(surface: RelayInstallSurface) -> None:
    with pytest.raises(RelayCliJobError) as excinfo:
        await surface.invoke("relay_cluster_bootstrap", {"action": "status", "job_id": "nope"})
    assert excinfo.value.reason == "relay_install_job_not_found"


# --------------------------------------------------------------------------- #
# relay_cluster_status (bounded 3-way composition)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cluster_status_composes_three_subprobes_independently(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST: one failing sub-probe (proxy-status) never masks or fails the
    other two (doctor, installation-info) -- each surfaces its own typed status."""

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
async def test_session_lifecycle_teardown_scheduler_cancel_requires_cancel_jobs(
    surface: RelayInstallSurface,
) -> None:
    """FAILING-FIRST: cancel_scheduler_jobs without cancel_jobs is a typed
    validation refusal (mirrors clio-relay's own CLI precondition), never silently
    dropped or forwarded as an invalid flag combination."""

    with pytest.raises(RelayCliJobError) as excinfo:
        await surface.invoke(
            "relay_session_lifecycle",
            {
                "action": "teardown",
                "cluster": "demo",
                "session_id": "s1",
                "cancel_scheduler_jobs": True,
            },
        )
    assert excinfo.value.reason == "relay_install_arguments_invalid"


@pytest.mark.asyncio
async def test_session_lifecycle_missing_cluster_is_typed(surface: RelayInstallSurface) -> None:
    with pytest.raises(RelayCliJobError) as excinfo:
        await surface.invoke("relay_session_lifecycle", {"action": "attach"})
    assert excinfo.value.reason == "relay_install_arguments_invalid"


# --------------------------------------------------------------------------- #
# relay_proxy_lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proxy_lifecycle_install_lingering_gate_actionable_refusal(
    surface: RelayInstallSurface, fake_relay_cli: ScenarioSetter
) -> None:
    """FAILING-FIRST: exit-78's lingering gate (surfaced by clio-relay as a bare
    nonzero exit + a known stderr signature) becomes a typed actionable_refusal
    naming the enable-linger remediation on the terminal job result."""

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
async def test_proxy_lifecycle_missing_cluster_is_typed(surface: RelayInstallSurface) -> None:
    with pytest.raises(RelayCliJobError) as excinfo:
        await surface.invoke("relay_proxy_lifecycle", {"action": "install_proxy"})
    assert excinfo.value.reason == "relay_install_arguments_invalid"


# --------------------------------------------------------------------------- #
# tool mounting shape
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
