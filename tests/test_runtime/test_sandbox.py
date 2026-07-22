"""OS write-confinement ladder + seam wiring on the typed floor (#974/#975, B1).

The confinement plumbing lands FLOOR-FIRST: :func:`wrap_confined` always resolves to the
passthrough ``none`` backend this slice, so every spawn is byte-identical to today while
the mechanism labels, doctor row and provenance field populate. These tests pin:

* the backend ladder's typed reasons for every rung (incl. ``srt_not_installed``),
* that the srt VERSION probe reads ``package.json`` — never the lying ``srt --version``,
* the ``pdeathsig`` fold preserving argv EXACTLY where it applies today,
* the spawn-diet FINAL argv being what gets wrapped (not the launcher chain),
* the exclusion classification (CTE daemon / provider CLIs never wrapped),
* the ``effective_write_roots`` boundary sharing file_policy's source + the uv cache,
* the doctor row being DEGRADED (never ERROR) on the floor,
* and the provenance environment field + shell sabotage floor.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent.runtime import sandbox
from clio_agent.runtime.status import IntegrationState

# --------------------------------------------------------------------------- #
# Mechanism labels + classification                                            #
# --------------------------------------------------------------------------- #


def test_known_mechanisms_are_exactly_the_ladder_rungs() -> None:
    """The typed mechanism set is the whole ladder (srt family + Landlock + none)."""
    assert sandbox.KNOWN_MECHANISMS == frozenset(
        {
            sandbox.MECHANISM_SRT_SEATBELT,
            sandbox.MECHANISM_SRT_BWRAP,
            sandbox.MECHANISM_SRT_WINDOWS,
            sandbox.MECHANISM_LANDLOCK,
            sandbox.MECHANISM_NONE,
        }
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("mcp_stdio", sandbox.CONFINEMENT_WRAPPED),
        ("mcp_launcher", sandbox.CONFINEMENT_WRAPPED),
        ("python_child", sandbox.CONFINEMENT_WRAPPED),
        ("clio_core_daemon", sandbox.CONFINEMENT_EXCLUDED),
        ("sdk_cli", sandbox.CONFINEMENT_EXCLUDED),
        ("codex_cli", sandbox.CONFINEMENT_EXCLUDED),
        ("other", sandbox.CONFINEMENT_EXCLUDED),
    ],
)
def test_confinement_classification_names_the_excluded_seams(kind: str, expected: str) -> None:
    """The CTE daemon + provider CLI links are verifiably EXCLUDED; MCP kinds wrapped."""
    assert sandbox.confinement_for_kind(kind) == expected


def test_excluded_seams_are_never_wrapped() -> None:
    """CTE daemon / provider pools / serve-shaped children are never confined (owner #974.5)."""
    for kind in ("clio_core_daemon", "sdk_cli", "codex_cli"):
        assert sandbox.confinement_for_kind(kind) == sandbox.CONFINEMENT_EXCLUDED


# --------------------------------------------------------------------------- #
# srt detection — the ladder rungs, and the version-probe defect guard         #
# --------------------------------------------------------------------------- #


def _which_none(_name: str) -> str | None:
    return None


def test_detect_srt_not_installed_when_binary_absent() -> None:
    """No ``srt`` on PATH → the typed ``srt_not_installed`` rung (detection only)."""
    det = sandbox.detect_srt(which=_which_none, platform="linux")
    assert det.installed is False
    assert det.reason == sandbox.REASON_SRT_NOT_INSTALLED


def test_detect_srt_node_missing() -> None:
    """srt present but node absent → ``srt_node_missing``."""
    which = {"srt": "/opt/srt"}.get
    det = sandbox.detect_srt(
        which=lambda n: which(n),
        package_version=lambda _p: "0.0.66",
        platform="linux",
    )
    assert det.installed is True
    assert det.reason == sandbox.REASON_SRT_NODE_MISSING


def test_detect_srt_node_too_old() -> None:
    """node below 20.11 → ``srt_node_too_old`` (owner note #974)."""
    which = {"srt": "/opt/srt", "node": "/usr/bin/node", "socat": "/usr/bin/socat"}.get
    det = sandbox.detect_srt(
        which=lambda n: which(n),
        package_version=lambda _p: "0.0.66",
        node_version_reader=lambda: "v18.19.0",
        platform="linux",
    )
    assert det.node_ok is False
    assert det.reason == sandbox.REASON_SRT_NODE_TOO_OLD


def test_detect_srt_node_version_unreadable_is_not_too_old() -> None:
    """node on PATH but its version unreadable → ``srt_node_version_unreadable``.

    An unreadable version is NOT the claim "too old" (review fix, #975): the probe
    failure is logged by the reader and the typed reason says what actually happened.
    """
    which = {"srt": "/opt/srt", "node": "/usr/bin/node", "socat": "/usr/bin/socat"}.get
    det = sandbox.detect_srt(
        which=lambda n: which(n),
        package_version=lambda _p: "0.0.66",
        node_version_reader=lambda: "",
        platform="linux",
    )
    assert det.node_present is True
    assert det.node_ok is False
    assert det.reason == sandbox.REASON_SRT_NODE_VERSION_UNREADABLE
    assert det.reason != sandbox.REASON_SRT_NODE_TOO_OLD


def test_sandbox_state_event_is_declared_trace_only() -> None:
    """``sandbox.state`` is in ``SSE_TRACE_ONLY_EVENT_TYPES`` — trace-only by declaration.

    Without this registration the boot event stays off SSE only by accident of its
    hardcoded ``completed`` status; a future ``failed`` emit would ride the
    ``_SSE_ALWAYS_STATUSES`` override onto the live wire (the S5-gate3-C5 leak class).
    """
    from clio_agent.gact.semantic_events import (
        SSE_TRACE_ONLY_EVENT_TYPES,
        event_reaches_ui,
    )

    assert "sandbox.state" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("sandbox.state") is False
    # The sharp edge: even a failure-status emit must NOT reach the UI wire.
    assert event_reaches_ui("sandbox.state", status="failed") is False


def test_detect_srt_socat_missing_on_linux() -> None:
    """On Linux, srt + modern node but no socat → ``srt_socat_missing``."""
    which = {"srt": "/opt/srt", "node": "/usr/bin/node"}.get
    det = sandbox.detect_srt(
        which=lambda n: which(n),
        package_version=lambda _p: "0.0.66",
        node_version_reader=lambda: "v20.11.0",
        platform="linux",
    )
    assert det.socat_present is False
    assert det.reason == sandbox.REASON_SRT_SOCAT_MISSING


def test_detect_srt_all_present_defers_activation() -> None:
    """srt + node>=20.11 + socat → detected-but-deferred (this slice never activates)."""
    which = {"srt": "/opt/srt", "node": "/usr/bin/node", "socat": "/usr/bin/socat"}.get
    det = sandbox.detect_srt(
        which=lambda n: which(n),
        package_version=lambda _p: "0.0.66",
        node_version_reader=lambda: "v22.2.0",
        platform="linux",
    )
    assert det.version == "0.0.66"
    assert det.reason == sandbox.REASON_SRT_DETECTED_DEFERRED


def test_srt_version_read_from_package_json_never_the_lying_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version probe reads ``package.json`` — NEVER ``srt --version`` (which lies 1.0.0).

    A probe that trusted the CLI banner would be a defect (owner note #974): ``srt
    --version`` prints a stale ``1.0.0``. We lay down the real npm layout with version
    ``0.0.66`` and a shim whose (hypothetical) banner would say ``1.0.0``; the probe must
    return ``0.0.66`` and must never shell out to the binary.
    """
    pkg_dir = tmp_path / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": sandbox.SRT_PACKAGE_NAME, "version": "0.0.66"}), encoding="utf-8"
    )
    binary = tmp_path / "node_modules" / ".bin" / "srt"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho 1.0.0\n", encoding="utf-8")

    # If the probe EVER spawned the binary, this tripwire raises. The probe is a pure
    # filesystem read of package.json; it must never shell out (that would trust the lie).
    def _no_spawn(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
        raise AssertionError("srt version probe must not spawn the binary (--version lies)")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    version = sandbox._srt_package_version(str(binary))
    assert version == "0.0.66"
    assert version != "1.0.0"  # never the banner


# --------------------------------------------------------------------------- #
# Backend resolution per platform (monkeypatched) — always ``none`` this slice #
# --------------------------------------------------------------------------- #


def _det(reason: str, *, installed: bool = False, version: str = "") -> sandbox.SrtDetection:
    return sandbox.SrtDetection(
        installed=installed,
        binary_path="/opt/srt" if installed else "",
        version=version,
        node_present=installed,
        node_version="v22.0.0" if installed else "",
        node_ok=installed,
        socat_present=installed,
        reason=reason,
    )


def _ll(available: bool, *, abi: int = 0, reason: str = ""):
    from clio_agent.runtime.sandbox_landlock import LandlockProbe

    return LandlockProbe(available=available, abi=abi, refer_supported=abi >= 2, reason=reason)


def test_resolve_backend_linux_floor_is_srt_not_installed() -> None:
    """Linux, srt absent AND Landlock absent → floor ``none`` (B2 ladder bottom).

    The floor reason is the last-rung (Landlock) reason; the srt skip is preserved in
    ``details.srt_skip_reason`` so both rungs are honest.
    """
    result = sandbox._resolve_backend(
        platform="linux",
        detection=_det(sandbox.REASON_SRT_NOT_INSTALLED),
        landlock=_ll(False, reason=sandbox.REASON_LANDLOCK_UNAVAILABLE),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == sandbox.REASON_LANDLOCK_UNAVAILABLE
    assert result.details["srt_skip_reason"] == sandbox.REASON_SRT_NOT_INSTALLED
    assert result.details["target_mechanism"] == sandbox.MECHANISM_SRT_BWRAP


def test_resolve_backend_windows_absent_is_unprovisioned() -> None:
    """Windows, srt absent → the honest provisioning gate reason (owner #974.2)."""
    result = sandbox._resolve_backend(
        platform="win32", detection=_det(sandbox.REASON_SRT_NOT_INSTALLED)
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_WINDOWS_UNPROVISIONED
    assert result.details["target_mechanism"] == sandbox.MECHANISM_SRT_WINDOWS


def test_resolve_backend_darwin_floor() -> None:
    """macOS target is Seatbelt; absent srt still floors to ``none``."""
    result = sandbox._resolve_backend(
        platform="darwin", detection=_det(sandbox.REASON_SRT_NOT_INSTALLED)
    )
    assert result.details["target_mechanism"] == sandbox.MECHANISM_SRT_SEATBELT
    assert result.reason == sandbox.REASON_SRT_NOT_INSTALLED


def test_resolve_backend_srt_present_and_bwrap_ok_activates_srt() -> None:
    """B2: srt viable + bwrap ok → ACTIVATE srt_bwrap (proxy started, net=proxy)."""
    result = sandbox._resolve_backend(
        platform="linux",
        detection=_det(sandbox.REASON_SRT_DETECTED_DEFERRED, installed=True, version="0.0.66"),
        bwrap=(True, ""),
        start_proxy=lambda: 51515,
    )
    assert result.mechanism == sandbox.MECHANISM_SRT_BWRAP
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["proxy_port"] == 51515
    assert result.details["net_enforcement"] == sandbox.NET_ENFORCEMENT_PROXY
    assert result.details["srt_binary"] == "/opt/srt"


def test_resolve_backend_disabled_by_config() -> None:
    """A disabled knob stamps ``disabled_by_config`` (no silent no-op)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "false"},
        platform="linux",
        detection=_det(sandbox.REASON_SRT_NOT_INSTALLED),
    )
    assert result.reason == sandbox.REASON_DISABLED


def test_install_and_current_state_cache() -> None:
    """install_sandbox resolves + caches; current_state returns the cached result.

    B2 may ACTIVATE a fence on a capable host (Linux+srt/Landlock), so this pins the
    cache contract + a typed reason, not a floor mechanism (that is host-dependent now).
    """
    result = sandbox.install_sandbox()
    assert result.mechanism in sandbox.KNOWN_MECHANISMS
    assert result.reason  # a typed reason, never blank
    assert sandbox.current_state() is result


# --------------------------------------------------------------------------- #
# wrap_confined — the single composer; passthrough + the pdeathsig fold         #
# --------------------------------------------------------------------------- #


def test_wrap_confined_passthrough_is_byte_identical() -> None:
    """On the floor, no fence prefix, no env overlay, no extra popen kwargs."""
    confined = sandbox.wrap_confined("mytool", ["--x", "1"], profile=sandbox.PROFILE_FLEET)
    assert confined.command == "mytool"
    assert confined.args == ["--x", "1"]
    assert confined.env_overlay == {}
    assert confined.popen_kwargs == {}
    assert confined.result.mechanism == sandbox.MECHANISM_NONE
    # per-call intent is recorded on the result details (typed, never activated).
    assert confined.result.details["profile"] == sandbox.PROFILE_FLEET
    assert confined.result.details["net_policy"] == sandbox.NET_ALLOW_RECORD
    assert confined.result.details["pdeathsig"] is False


def test_pdeathsig_fold_preserves_argv_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The folded pdeathsig prefix is IDENTICAL to the legacy helper, on every platform.

    Owner decision #974.5: pdeathsig folds into wrap_confined as the OUTERMOST composer
    step, one prefix owner. This pins that the composed argv equals
    ``pdeathsig_wrapped_command`` exactly — both where it applies (Linux + setpriv) and
    where it is a no-op passthrough (non-Linux / no setpriv).
    """
    from clio_agent.tools import mcp_config

    # Force the Linux + setpriv-present branch so the prefix actually appears.
    monkeypatch.setattr(mcp_config.sys, "platform", "linux")
    monkeypatch.setattr(mcp_config.shutil, "which", lambda _n: "/usr/bin/setpriv")
    legacy = mcp_config.pdeathsig_wrapped_command("mytool", ["--x", "1"])
    confined = sandbox.wrap_confined(
        "mytool", ["--x", "1"], profile=sandbox.PROFILE_FLEET, pdeathsig=True
    )
    assert (confined.command, confined.args) == legacy
    assert confined.command == "/usr/bin/setpriv"
    assert confined.args[:3] == ["--pdeathsig", "SIGKILL", "--"]

    # Non-Linux: the helper is a passthrough, and so is the fold (byte-identical).
    monkeypatch.setattr(mcp_config.sys, "platform", "win32")
    legacy_win = mcp_config.pdeathsig_wrapped_command("mytool", ["--x", "1"])
    confined_win = sandbox.wrap_confined(
        "mytool", ["--x", "1"], profile=sandbox.PROFILE_FLEET, pdeathsig=True
    )
    assert (confined_win.command, confined_win.args) == legacy_win == ("mytool", ["--x", "1"])


# --------------------------------------------------------------------------- #
# Seam wiring — the spawn-diet FINAL argv is what gets wrapped                  #
# --------------------------------------------------------------------------- #


def test_transport_for_wraps_the_spawn_diet_final_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence wraps the FINAL (post-diet) argv, not the deleted launcher chain (#975).

    Force a spawn-diet plan; transport_for must build the StdioTransport from the DIET
    command/args (fleet profile, no pdeathsig today — so no setpriv prefix even here).
    """
    from clio_agent.tools import mcp_config, spawn_diet
    from clio_agent.tools.mcp_config import MCPServerSpec, transport_for

    spec = MCPServerSpec(
        name="demo", transport="stdio", command="python", args=["-m", "demo"], env={}, source="test"
    )
    monkeypatch.setattr(mcp_config.shutil, "which", lambda _n: "/usr/bin/python")

    diet_plan = ("/venv/bin/python", ["-m", "demo_direct"], {"UV_CACHE_DIR": "/x", "PATH": "/p"})
    monkeypatch.setattr(
        spawn_diet, "diet_transport_args", lambda *_a, **_k: diet_plan, raising=True
    )
    # setpriv present would prove pdeathsig is OFF for this seam if it leaked in.
    monkeypatch.setattr(mcp_config.sys, "platform", "linux")

    transport = transport_for(spec, cwd=str(tmp_path))
    assert transport.command == "/venv/bin/python"  # diet command, not the launcher
    assert transport.args == ["-m", "demo_direct"]
    assert "setpriv" not in transport.command  # transport_for carries no pdeathsig today


def test_transport_from_spec_matches_legacy_pdeathsig_exactly() -> None:
    """The dict-spec seam produces the SAME argv the direct pdeathsig helper did (parity)."""
    from clio_agent.tools.mcp_config import pdeathsig_wrapped_command, transport_from_spec

    transport = transport_from_spec(
        {"transport": "stdio", "command": "mytool", "args": ["--x", "1"], "env": {"A": "b"}}
    )
    legacy_cmd, legacy_args = pdeathsig_wrapped_command("mytool", ["--x", "1"])
    assert transport.command == legacy_cmd
    assert transport.args == legacy_args
    assert transport.env == {"A": "b"}  # env preserved (no overlay on the floor)


# --------------------------------------------------------------------------- #
# effective_write_roots — the ONE shared boundary + the uv-cache false-positive #
# --------------------------------------------------------------------------- #


def test_fleet_roots_include_the_mcp_uv_cache() -> None:
    """The fleet fence must include the mcp-uv-cache so a uv launcher write never trips."""
    from clio_agent.tools.mcp_config import _mcp_uv_cache_dir

    roots = sandbox.effective_write_roots(sandbox.PROFILE_FLEET)
    assert _mcp_uv_cache_dir() in roots


def test_effective_write_roots_share_file_policy_source() -> None:
    """The advisory allowed_roots and the fence write_roots derive from ONE source (#974.6).

    The fence territory is a SUPERSET of file_policy's allowed_roots (it only ever adds
    caches the launcher needs), so the two boundaries can never drift apart.
    """
    from clio_agent.tools.file_policy import FileAccessPolicy

    policy = FileAccessPolicy.from_env()
    roots = sandbox.effective_write_roots(sandbox.PROFILE_FLEET, policy=policy)
    assert set(policy.allowed_roots) <= set(roots)
    # And the advisory base leads the ordering (shared source first).
    assert roots[: len(policy.allowed_roots)] == policy.allowed_roots


def test_shell_roots_do_not_pull_in_the_fleet_tool_caches() -> None:
    """The per-invocation shell profile is narrower than fleet (no mcp-uv-cache)."""
    from clio_agent.tools.mcp_config import _mcp_uv_cache_dir

    shell_roots = sandbox.effective_write_roots(sandbox.PROFILE_SHELL)
    assert _mcp_uv_cache_dir() not in shell_roots


# --------------------------------------------------------------------------- #
# Doctor row — DEGRADED never ERROR (the floor is legal)                        #
# --------------------------------------------------------------------------- #


def test_probe_sandbox_floor_is_degraded_never_error() -> None:
    """No fence is a LEGAL config: the row is DEGRADED, never UNAVAILABLE/MISCONFIGURED."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE,
        active=False,
        reason=sandbox.REASON_SRT_NOT_INSTALLED,
        details={"srt": {"version": ""}},
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.name == "sandbox"
    assert row.state == IntegrationState.DEGRADED
    assert row.state not in {IntegrationState.UNAVAILABLE, IntegrationState.MISCONFIGURED}
    assert row.details["reason"] == sandbox.REASON_SRT_NOT_INSTALLED
    assert row.required is False


def test_probe_sandbox_reports_mechanism_and_reason() -> None:
    """The row surfaces the mechanism label + typed reason for the trace/doctor."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE,
        active=False,
        reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
        details={},
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.details["mechanism"] == sandbox.MECHANISM_NONE
    assert "clio sandbox setup" in row.next_action


def test_probe_sandbox_skipped_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standalone doctor with no boot resolution reports SKIPPED, not a false green."""
    monkeypatch.setattr(sandbox, "_STATE", None)
    row = sandbox.probe_sandbox()
    assert row.state == IntegrationState.SKIPPED


# --------------------------------------------------------------------------- #
# Process census — the confinement column makes exclusion visible policy        #
# --------------------------------------------------------------------------- #


def test_census_classify_stamps_confinement_column() -> None:
    """classify_parentage labels each CLIO process wrapped/excluded (#975)."""
    from clio_agent.runtime import process_census as pc

    nodes = [
        pc.ProcessNode(pid=1, ppid=0, name="clio-agent", create_time=0.0, kind="other"),
        pc.ProcessNode(pid=10, ppid=1, name="clio-kit", create_time=0.0, kind="mcp_stdio"),
        pc.ProcessNode(pid=11, ppid=1, name="claude", create_time=0.0, kind="sdk_cli"),
        pc.ProcessNode(pid=12, ppid=1, name="clio_run", create_time=0.0, kind="clio_core_daemon"),
    ]
    rows = pc.classify_parentage(nodes, server_root_pid=1, daemon_root_pid=None)
    by_pid = {r.pid: r for r in rows}
    assert by_pid[10].confinement == sandbox.CONFINEMENT_WRAPPED
    assert by_pid[11].confinement == sandbox.CONFINEMENT_EXCLUDED
    assert by_pid[12].confinement == sandbox.CONFINEMENT_EXCLUDED


# --------------------------------------------------------------------------- #
# Provenance environment field carries the sandbox floor (#966 tie-in)          #
# --------------------------------------------------------------------------- #


def test_capture_environment_stamps_sandbox_floor(tmp_path: Path) -> None:
    """A transform's environment carries the resolved ``none/<reason>`` — the honest floor.

    This is what turns #966's ``gap`` node into an attributable record without inventing
    new vocabulary: the mechanism label is already ON the record, floor-first.
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.environment import capture_environment

    sandbox.install_sandbox()  # resolve the floor state for THIS process
    env = capture_environment(FastAPI())
    assert env.sandbox_mechanism == sandbox.MECHANISM_NONE
    assert env.sandbox_reason  # a typed, non-empty reason (never silently blank)


def test_environment_payload_roundtrips_sandbox_fields() -> None:
    """The folded environment payload preserves the sandbox fields (boot-fold tolerant)."""
    from clio_agent.gact.artifacts.environment import (
        EnvironmentRecord,
        environment_from_payload,
    )

    original = EnvironmentRecord(
        sandbox_mechanism=sandbox.MECHANISM_NONE, sandbox_reason=sandbox.REASON_SRT_NOT_INSTALLED
    )
    rebuilt = environment_from_payload(original.model_dump())
    assert rebuilt.sandbox_mechanism == sandbox.MECHANISM_NONE
    assert rebuilt.sandbox_reason == sandbox.REASON_SRT_NOT_INSTALLED


# --------------------------------------------------------------------------- #
# Sabotage floor — an out-of-root write SUCCEEDS (no fence), record says none    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_shell_out_of_root_write_succeeds_on_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor lets an out-of-root shell write through — and the record says so (#975).

    The shell server validates only its ``cwd``; the command it runs writes wherever it
    likes with no per-write fence this slice. We prove the write SUCCEEDS from an allowed
    cwd into an OUTSIDE path, and that the resolved confinement state is the honest
    ``none/<reason>`` — the toothless-but-honest floor the campaign then makes true.
    """
    from clio_agent.tools.servers.shell_server import shell_server

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(allowed))
    from clio_agent import conf

    conf.reload()
    # Force the honest FLOOR state so this floor test is deterministic regardless of host —
    # on a fenced Linux CI box B2 would otherwise ACTIVATE a backend and deny the write.
    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE,
            active=False,
            reason=sandbox.REASON_SRT_NOT_INSTALLED,
            details={"platform": sys.platform},
        ),
    )

    target = outside / "escaped.txt"
    # Portable out-of-root write via the interpreter (avoids shell-dialect differences).
    py = sys.executable.replace("\\", "/")
    tgt = str(target).replace("\\", "/")
    if sys.platform.startswith("win"):
        command = f"& '{py}' -c \"open(r'{tgt}','w').write('escaped')\""
    else:
        command = f"'{py}' -c \"open('{tgt}','w').write('escaped')\""

    try:
        async with Client(shell_server) as client:
            result = await client.call_tool(
                "bash", {"command": command, "cwd": str(allowed), "timeout_s": 30}
            )
    finally:
        conf.reload()

    data = getattr(result, "data", result)
    if isinstance(data, str):
        data = json.loads(data)
    assert data["exit_code"] == 0, data
    assert target.read_text(encoding="utf-8") == "escaped"  # the forced floor did NOT stop it

    # The forced floor state is what governed the spawn (no OS fence, honest reason).
    state = sandbox.current_state()
    assert state is not None
    assert state.mechanism == sandbox.MECHANISM_NONE
    assert state.reason  # a typed reason on the record, never blank
