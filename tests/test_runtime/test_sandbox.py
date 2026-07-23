"""OS write-confinement ladder + seam wiring on the typed floor (#974/#975, B-codex-5).

The confinement plumbing lands FLOOR-FIRST: :func:`wrap_confined` resolves to the passthrough
``none`` backend where no OS fence is available, so every spawn is byte-identical to today while
the mechanism labels, doctor row and provenance field populate. These tests pin:

* the backend ladder's typed reasons (codex-primary → Landlock on Linux → floor),
* the ``pdeathsig`` fold preserving argv EXACTLY where it applies today,
* the spawn-diet FINAL argv being what gets wrapped (not the launcher chain),
* the exclusion classification (CTE daemon / provider CLIs never wrapped),
* the ``effective_write_roots`` boundary sharing file_policy's source + the uv cache,
* the doctor row being DEGRADED (never ERROR) on the floor,
* and the provenance environment field + shell sabotage floor.

Codex-specific detection / provisioning / ladder-gate coverage lives in
``test_sandbox_codex_ladder.py`` and ``test_sandbox_codex_provision.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from clio_agent.runtime import sandbox
from clio_agent.runtime import sandbox_codex as sc
from clio_agent.runtime.sandbox_landlock import LandlockProbe
from clio_agent.runtime.status import IntegrationState

# --------------------------------------------------------------------------- #
# Mechanism labels + classification                                            #
# --------------------------------------------------------------------------- #


def test_known_mechanisms_are_exactly_the_ladder_rungs() -> None:
    """The typed mechanism set is the whole ladder (Codex + Landlock + none)."""
    assert sandbox.KNOWN_MECHANISMS == frozenset(
        {
            sandbox.MECHANISM_CODEX,
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


# --------------------------------------------------------------------------- #
# Backend resolution per platform (injected probes) — codex primary, LL, floor #
# --------------------------------------------------------------------------- #


def _codex(reason: str, *, installed: bool = False, version: str = "") -> sc.CodexDetection:
    return sc.CodexDetection(
        installed=installed,
        binary_path="/usr/bin/codex" if installed else "",
        version=version,
        reason=reason,
    )


def _codex_ok() -> sc.CodexDetection:
    return _codex(sc.REASON_CODEX_DETECTED, installed=True, version="0.145.0")


def _ll(available: bool, *, abi: int = 0, reason: str = "") -> LandlockProbe:
    return LandlockProbe(available=available, abi=abi, refer_supported=abi >= 2, reason=reason)


def test_resolve_backend_linux_codex_absent_falls_to_landlock() -> None:
    """Linux, codex absent but Landlock present → the Landlock fallback rung activates."""
    result = sandbox._resolve_backend(
        platform="linux",
        codex_detection=_codex(sc.REASON_CODEX_NOT_INSTALLED),
        landlock=_ll(True, abi=2),
    )
    assert result.mechanism == sandbox.MECHANISM_LANDLOCK
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["codex_skip_reason"] == sc.REASON_CODEX_NOT_INSTALLED


def test_resolve_backend_linux_floor_is_landlock_reason() -> None:
    """Linux, codex absent AND Landlock absent → floor ``none`` (ladder bottom).

    The floor reason is the last-rung (Landlock) reason; the codex skip is preserved in
    ``details.codex_skip_reason`` so both rungs are honest.
    """
    result = sandbox._resolve_backend(
        platform="linux",
        codex_detection=_codex(sc.REASON_CODEX_NOT_INSTALLED),
        landlock=_ll(False, reason=sandbox.REASON_LANDLOCK_UNAVAILABLE),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == sandbox.REASON_LANDLOCK_UNAVAILABLE
    assert result.details["codex_skip_reason"] == sc.REASON_CODEX_NOT_INSTALLED
    assert result.details["target_mechanism"] == sandbox.MECHANISM_CODEX


def test_resolve_backend_windows_codex_absent_floors_typed() -> None:
    """Windows, codex absent → the honest codex precondition reason (no Landlock rung on win32)."""
    result = sandbox._resolve_backend(
        platform="win32",
        codex_detection=_codex(sc.REASON_CODEX_NOT_INSTALLED),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sc.REASON_CODEX_NOT_INSTALLED
    assert result.details["target_mechanism"] == sandbox.MECHANISM_CODEX


def test_resolve_backend_darwin_floor() -> None:
    """macOS: codex absent floors to ``none`` (no Landlock rung on darwin)."""
    result = sandbox._resolve_backend(
        platform="darwin", codex_detection=_codex(sc.REASON_CODEX_NOT_INSTALLED)
    )
    assert result.details["target_mechanism"] == sandbox.MECHANISM_CODEX
    assert result.reason == sc.REASON_CODEX_NOT_INSTALLED


def test_resolve_backend_codex_primary_activates_off_win32() -> None:
    """Codex viable off-win32 → MECHANISM_CODEX active (the primary backend, no gate)."""
    result = sandbox._resolve_backend(
        platform="linux",
        codex_detection=_codex_ok(),
        landlock=_ll(True),  # present, but codex is primary → never consulted
    )
    assert result.mechanism == sandbox.MECHANISM_CODEX
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["codex_binary"] == "/usr/bin/codex"
    assert result.details["net_enforcement"] == "codex-net-deferred"
    assert "landlock" not in result.details  # the fallback rung was never taken


def test_resolve_backend_disabled_by_config() -> None:
    """A disabled knob stamps ``disabled_by_config`` (no silent no-op)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "false"},
        platform="linux",
        codex_detection=_codex_ok(),
    )
    assert result.reason == sandbox.REASON_DISABLED


def test_install_and_current_state_cache() -> None:
    """install_sandbox resolves + caches; current_state returns the cached result.

    The ladder may ACTIVATE a fence on a capable host (codex or Landlock), so this pins the
    cache contract + a typed reason, not a floor mechanism (that is host-dependent now).
    """
    result = sandbox.install_sandbox()
    assert result.mechanism in sandbox.KNOWN_MECHANISMS
    assert result.reason  # a typed reason, never blank
    assert sandbox.current_state() is result


# --------------------------------------------------------------------------- #
# wrap_confined — the single composer; passthrough + the pdeathsig fold         #
# --------------------------------------------------------------------------- #


def test_wrap_confined_passthrough_is_byte_identical(floor_sandbox) -> None:
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


def test_pdeathsig_fold_preserves_argv_exactly(
    monkeypatch: pytest.MonkeyPatch, floor_sandbox
) -> None:
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, floor_sandbox
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


def test_transport_from_spec_matches_legacy_pdeathsig_exactly(floor_sandbox) -> None:
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


def test_fleet_roots_include_the_uv_tool_install_and_clio_kit_dirs() -> None:
    """The fleet fence must grant the uv DATA dir + clio-kit dirs (B2 live-gate regression).

    The shipped fleet launcher is ``clio-kit`` (``uv tool install``), and launching an MCP
    server builds that server's package IN-PLACE inside the uv tool install tree and writes
    uv temp files there — an active fence that granted only the caches denied the build
    (EROFS) and the whole fleet failed to start. The unit false-positive suite used a fixture
    server and missed it; the Linux live gate caught it. This pins the fix.
    """
    import platformdirs

    roots = set(sandbox.effective_write_roots(sandbox.PROFILE_FLEET))
    uv_data = Path(platformdirs.user_data_dir("uv", appauthor=False))
    clio_kit_cache = Path(platformdirs.user_cache_dir("clio-kit", appauthor=False))
    assert uv_data in roots, f"uv data/tools dir not in fleet territory: {sorted(map(str, roots))}"
    assert clio_kit_cache in roots, "clio-kit cache dir not in fleet territory"
    # The shell profile stays narrow — the launcher toolchain dirs are a fleet concern only.
    shell_roots = set(sandbox.effective_write_roots(sandbox.PROFILE_SHELL))
    assert uv_data not in shell_roots


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
        reason=sc.REASON_CODEX_NOT_INSTALLED,
        details={"codex": {"version": ""}},
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.name == "sandbox"
    assert row.state == IntegrationState.DEGRADED
    assert row.state not in {IntegrationState.UNAVAILABLE, IntegrationState.MISCONFIGURED}
    assert row.details["reason"] == sc.REASON_CODEX_NOT_INSTALLED
    assert row.required is False


def test_probe_sandbox_reports_mechanism_and_reason() -> None:
    """The row surfaces the mechanism label + typed reason for the trace/doctor."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE,
        active=False,
        reason=sc.REASON_CODEX_WINDOWS_UNPROVISIONED,
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


def test_capture_environment_stamps_sandbox_floor(tmp_path: Path, floor_sandbox) -> None:
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
        sandbox_mechanism=sandbox.MECHANISM_NONE, sandbox_reason=sc.REASON_CODEX_NOT_INSTALLED
    )
    rebuilt = environment_from_payload(original.model_dump())
    assert rebuilt.sandbox_mechanism == sandbox.MECHANISM_NONE
    assert rebuilt.sandbox_reason == sc.REASON_CODEX_NOT_INSTALLED


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
    # on a fenced host the ladder would otherwise ACTIVATE a backend and deny the write.
    monkeypatch.setattr(
        sandbox,
        "_STATE",
        sandbox.SandboxResult(
            mechanism=sandbox.MECHANISM_NONE,
            active=False,
            reason=sc.REASON_CODEX_NOT_INSTALLED,
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
