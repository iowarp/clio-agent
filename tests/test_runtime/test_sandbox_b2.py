"""B2 (#976): srt + Landlock fence activation, the degrade ladder, and gap→policy_violation.

Host-agnostic unit coverage (the Linux-fence sabotage suite is platform-marked, in
``test_sandbox_fence.py``, and runs in the live gate):

* srt config synthesis pinned against a golden (httpProxyPort + empty required arrays);
* clio-side schema validation rejects unknown/malformed synthesized config;
* srt version pin (supported floor);
* the srt argv PREFIX form ``srt -s <settings> --``;
* ladder selection pinned per (platform, srt, bwrap, landlock, enabled) matrix;
* argv composition order ``pdeathsig( srt( final-argv ) )`` and the Landlock shim;
* chokepoint-start failure drops the srt rung to Landlock (typed, never a silent net break);
* EROFS + EACCES → policy_violation mapping;
* the Landlock shim arg split + non-Linux probe.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from clio_agent.runtime import sandbox, sandbox_landlock, sandbox_srt
from clio_agent.runtime.sandbox_landlock import LandlockProbe

# --------------------------------------------------------------------------- #
# srt config synthesis + clio-side schema validation                           #
# --------------------------------------------------------------------------- #


def test_srt_config_synthesis_matches_golden() -> None:
    """The synthesized settings doc is exactly the pinned shape (owner note #974 spike)."""
    root = str(Path("/ws/project"))
    config = sandbox_srt.synthesize_srt_config([root], http_proxy_port=48080)
    assert config == {
        "network": {"allowedDomains": [], "deniedDomains": [], "httpProxyPort": 48080},
        "filesystem": {"denyRead": [], "allowWrite": [root], "denyWrite": []},
    }
    # Empty required arrays are valid; tlsTerminate is OFF by omission.
    sandbox_srt.validate_srt_config(config)  # must not raise


def test_srt_config_without_proxy_omits_httpproxyport() -> None:
    """No proxy port → the key is omitted (not null) and still schema-valid."""
    config = sandbox_srt.synthesize_srt_config([str(Path("/ws"))])
    assert "httpProxyPort" not in config["network"]
    sandbox_srt.validate_srt_config(config)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update({"bogus": 1}),  # unknown top-level key (srt would silently strip)
        lambda c: c["network"].update({"mitmProxy": {}}),  # unexpected network key
        lambda c: c["filesystem"].pop("denyRead"),  # missing required key
        lambda c: c["network"].update({"allowedDomains": ["*"]}),  # schema-forbidden '*'
        lambda c: c["filesystem"].update({"allowWrite": "not-a-list"}),  # wrong type
        lambda c: c["network"].update({"httpProxyPort": 0}),  # invalid port
    ],
)
def test_validate_srt_config_rejects_drift(mutate) -> None:
    """clio validates its OWN doc — srt's strip mode would otherwise swallow a drift/typo."""
    config = sandbox_srt.synthesize_srt_config([str(Path("/ws"))], http_proxy_port=1234)
    mutate(config)
    with pytest.raises(sandbox_srt.SrtConfigError) as exc:
        sandbox_srt.validate_srt_config(config)
    assert exc.value.reason == sandbox_srt.REASON_SRT_CONFIG_REJECTED


def test_write_settings_validates_before_disk(tmp_path: Path) -> None:
    """A rejected config never reaches disk (validation runs first)."""
    bad = {"network": {}, "filesystem": {}}
    target = tmp_path / "srt-settings" / "fleet.json"
    with pytest.raises(sandbox_srt.SrtConfigError):
        sandbox_srt.write_settings_file(bad, target)
    assert not target.exists()


def test_write_settings_roundtrips_good_config(tmp_path: Path) -> None:
    import json

    config = sandbox_srt.synthesize_srt_config([str(tmp_path)], http_proxy_port=9999)
    target = sandbox_srt.settings_path_for("fleet", cache_dir=tmp_path)
    written = sandbox_srt.write_settings_file(config, target)
    assert written.is_file()
    assert json.loads(written.read_text(encoding="utf-8")) == config


@pytest.mark.parametrize(
    ("version", "supported"),
    [("0.0.66", True), ("0.0.67", True), ("0.1.0", True), ("0.0.65", False), ("", False)],
)
def test_srt_version_pin(version: str, supported: bool) -> None:
    """The version pin is the schema-validated floor; below it (or unreadable) is unsupported."""
    assert sandbox_srt.is_srt_version_supported(version) is supported


def test_srt_prefix_argv_form() -> None:
    """The srt prefix is ``[binary, -s, <settings>, --]`` (argv passthrough, verified live)."""
    prefix = sandbox_srt.srt_prefix("/opt/srt", "/cache/fleet.json")
    assert prefix == ["/opt/srt", "-s", "/cache/fleet.json", "--"]


# --------------------------------------------------------------------------- #
# Ladder selection matrix — (platform, srt, bwrap, landlock, enabled)          #
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


def _srt_ok() -> sandbox.SrtDetection:
    return _det(sandbox.REASON_SRT_DETECTED_DEFERRED, installed=True, version="0.0.66")


def _ll(ok: bool) -> LandlockProbe:
    return LandlockProbe(
        available=ok,
        abi=1 if ok else 0,
        refer_supported=False,
        reason="" if ok else sandbox.REASON_LANDLOCK_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    ("platform", "det", "bwrap", "landlock_ok", "enabled", "mechanism", "active"),
    [
        # Linux: srt + bwrap ok → srt_bwrap.
        ("linux", _srt_ok(), (True, ""), True, True, sandbox.MECHANISM_SRT_BWRAP, True),
        # Linux: srt ok but bwrap broken (Ubuntu 24.04 AppArmor) → Landlock.
        (
            "linux",
            _srt_ok(),
            (False, sandbox.REASON_BWRAP_USERNS_RESTRICTED),
            True,
            True,
            sandbox.MECHANISM_LANDLOCK,
            True,
        ),
        # Linux: srt absent, Landlock present → Landlock.
        (
            "linux",
            _det(sandbox.REASON_SRT_NOT_INSTALLED),
            (True, ""),
            True,
            True,
            sandbox.MECHANISM_LANDLOCK,
            True,
        ),
        # Linux: neither → floor none.
        (
            "linux",
            _det(sandbox.REASON_SRT_NOT_INSTALLED),
            (False, sandbox.REASON_BWRAP_UNAVAILABLE),
            False,
            True,
            sandbox.MECHANISM_NONE,
            False,
        ),
        # Linux: srt version too old + no Landlock → floor none.
        (
            "linux",
            _det(sandbox.REASON_SRT_DETECTED_DEFERRED, installed=True, version="0.0.1"),
            (True, ""),
            False,
            True,
            sandbox.MECHANISM_NONE,
            False,
        ),
        # macOS: srt ok → Seatbelt.
        ("darwin", _srt_ok(), (True, ""), False, True, sandbox.MECHANISM_SRT_SEATBELT, True),
        # macOS: srt absent → floor (no Landlock rung on darwin).
        (
            "darwin",
            _det(sandbox.REASON_SRT_NOT_INSTALLED),
            (True, ""),
            True,
            True,
            sandbox.MECHANISM_NONE,
            False,
        ),
        # Windows: always floor this slice (activation is B3).
        ("win32", _srt_ok(), (True, ""), True, True, sandbox.MECHANISM_NONE, False),
        # Disabled knob → floor regardless of a fully-capable host.
        ("linux", _srt_ok(), (True, ""), True, False, sandbox.MECHANISM_NONE, False),
    ],
)
def test_ladder_selection_matrix(
    platform, det, bwrap, landlock_ok, enabled, mechanism, active
) -> None:
    """The full ladder decision table, pinned with injected probes (host-independent)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true" if enabled else "false"},
        platform=platform,
        detection=det,
        bwrap=bwrap,
        landlock=_ll(landlock_ok),
        start_proxy=lambda: 40000,
    )
    assert result.mechanism == mechanism
    assert result.active is active
    if active:
        assert result.reason == sandbox.REASON_FENCE_ACTIVE


def test_chokepoint_failure_drops_srt_rung_to_landlock() -> None:
    """A proxy that cannot start drops the srt rung to Landlock — typed, never a silent net break."""
    from clio_agent.runtime.net_chokepoint import ChokepointStartError

    def _boom() -> int:
        raise ChokepointStartError("bind failed")

    result = sandbox._resolve_backend(
        platform="linux",
        detection=_srt_ok(),
        bwrap=(True, ""),
        landlock=_ll(True),
        start_proxy=_boom,
    )
    assert result.mechanism == sandbox.MECHANISM_LANDLOCK
    assert result.active is True
    assert result.details["srt_skip_reason"] == sandbox.REASON_CHOKEPOINT_START_FAILED


def test_chokepoint_failure_on_darwin_floors() -> None:
    """No Landlock rung on macOS: a proxy failure floors with the typed chokepoint reason."""
    from clio_agent.runtime.net_chokepoint import ChokepointStartError

    def _boom() -> int:
        raise ChokepointStartError("bind failed")

    result = sandbox._resolve_backend(platform="darwin", detection=_srt_ok(), start_proxy=_boom)
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_CHOKEPOINT_START_FAILED


# --------------------------------------------------------------------------- #
# argv composition order — pdeathsig( fence( final-argv ) )                     #
# --------------------------------------------------------------------------- #


def _srt_state(port: int = 40000) -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_BWRAP,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"srt_binary": "/opt/srt", "proxy_port": port, "net_enforcement": "proxy"},
    )


def test_wrap_confined_srt_composes_prefix_inner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active srt state prepends ``srt -s <settings> --`` and writes a valid settings file."""
    monkeypatch.setattr(
        sandbox_srt,
        "settings_path_for",
        lambda profile, config=None, cache_dir=None: tmp_path / f"{profile}.json",
    )
    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        state=_srt_state(),
    )
    assert confined.command == "/opt/srt"
    assert confined.args[:3] == ["-s", str(tmp_path / "fleet.json"), "--"]
    assert confined.args[3:] == ["python", "-c", "print(1)"]
    # The settings the fence will read were validated + written.
    import json

    written = json.loads((tmp_path / "fleet.json").read_text(encoding="utf-8"))
    assert written["network"]["httpProxyPort"] == 40000
    assert written["filesystem"]["allowWrite"] == [str(tmp_path)]


def test_wrap_confined_pdeathsig_stays_outermost_over_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition order is ``pdeathsig( srt( argv ) )`` — pdeathsig OUTERMOST (owner #974.5)."""
    monkeypatch.setattr(
        sandbox_srt,
        "settings_path_for",
        lambda profile, config=None, cache_dir=None: tmp_path / f"{profile}.json",
    )
    from clio_agent.tools import mcp_config

    monkeypatch.setattr(mcp_config.sys, "platform", "linux")
    monkeypatch.setattr(mcp_config.shutil, "which", lambda _n: "/usr/bin/setpriv")
    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(tmp_path)],
        profile=sandbox.PROFILE_FLEET,
        pdeathsig=True,
        state=_srt_state(),
    )
    # setpriv (pdeathsig) is outermost, then srt, then the real argv.
    assert confined.command == "/usr/bin/setpriv"
    assert confined.args[:3] == ["--pdeathsig", "SIGKILL", "--"]
    assert confined.args[3] == "/opt/srt"
    assert confined.args[-3:] == ["python", "-c", "print(1)"]


def test_wrap_confined_landlock_composes_shim() -> None:
    """An active Landlock state prepends the ``landlock_exec`` shim over the roots."""
    import sys as _sys

    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": "env-cooperative"},
    )
    confined = sandbox.wrap_confined(
        "mytool",
        ["--x"],
        write_roots=[str(Path("/ws"))],
        profile=sandbox.PROFILE_SHELL,
        state=state,
    )
    assert confined.command == _sys.executable
    assert confined.args[:3] == ["-m", "clio_agent.runtime.landlock_exec", str(Path("/ws"))]
    assert confined.args[-3:] == ["--", "mytool", "--x"]


def test_wrap_confined_bad_srt_config_raises_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fence that cannot compose RAISES (typed) — never a silent unconfined spawn."""

    def _boom(*_a, **_k):
        raise sandbox_srt.SrtConfigError("synthesized bad")

    monkeypatch.setattr(sandbox_srt, "synthesize_srt_config", _boom)
    with pytest.raises(sandbox.SandboxCompositionError):
        sandbox.wrap_confined(
            "python", [], write_roots=["/ws"], profile=sandbox.PROFILE_FLEET, state=_srt_state()
        )


# --------------------------------------------------------------------------- #
# Landlock shim arg parsing + probe                                            #
# --------------------------------------------------------------------------- #


def test_landlock_exec_parse_argv_splits_on_separator() -> None:
    from clio_agent.runtime import landlock_exec

    roots, command = landlock_exec.parse_argv(["/ws", "/tmp", "--", "python", "-c", "x"])
    assert roots == ["/ws", "/tmp"]
    assert command == ["python", "-c", "x"]


@pytest.mark.parametrize("argv", [["/ws", "python"], ["/ws", "--"]])
def test_landlock_exec_parse_argv_rejects_malformed(argv) -> None:
    from clio_agent.runtime import landlock_exec

    with pytest.raises(ValueError):
        landlock_exec.parse_argv(argv)


def test_landlock_shim_prefix_form() -> None:
    prefix = sandbox_landlock.landlock_shim_prefix([Path("/ws")], python_exe="/py")
    assert prefix == ["/py", "-m", "clio_agent.runtime.landlock_exec", str(Path("/ws")), "--"]


def test_landlock_probe_non_linux_is_unavailable() -> None:
    probe = sandbox_landlock.probe_landlock(platform="win32")
    assert probe.available is False
    assert probe.reason == sandbox_landlock.REASON_LANDLOCK_UNAVAILABLE


# --------------------------------------------------------------------------- #
# gap → policy_violation: EROFS/EACCES mapping + the observer mint            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # bwrap denial (the spike's confirmed shape — strerror text, not the bracket).
        ("/bin/sh: 1: cannot create /home/u/out.txt: Read-only file system", "EROFS"),
        # Python OSError bracket form.
        (f"[Errno {errno.EROFS}] Read-only file system: '/etc/x'", "EROFS"),
        (f"[Errno {errno.EACCES}] Permission denied: '/root/y'", "EACCES"),
        ("PermissionError: [Errno 13] Permission denied", "EACCES"),
    ],
)
def test_write_denial_mapping_catches_erofs_and_eacces(text: str, expected: str) -> None:
    from clio_agent.gact.artifacts.violations import write_denial_from_result

    denial = write_denial_from_result({"stderr": text, "exit_code": 1})
    assert denial is not None
    assert denial["errno_name"] == expected


def test_write_denial_mapping_ignores_clean_result() -> None:
    from clio_agent.gact.artifacts.violations import write_denial_from_result

    assert write_denial_from_result({"stdout": "ok", "exit_code": 0}) is None


def test_observe_policy_violations_noop_on_floor() -> None:
    """The floor never mints a violation (its out-of-root write is an honest gap)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.violations import observe_policy_violations, policy_violations

    app = FastAPI()
    floor = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE, active=False, reason=sandbox.REASON_SRT_NOT_INSTALLED
    )
    out = observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": "cannot create /x: Read-only file system"},
        workspace_id="",
        state=floor,
    )
    assert out == []
    assert policy_violations(app) == []


def test_observe_policy_violations_mints_prevented_when_fenced() -> None:
    """A fenced EROFS result mints a ``prevented`` policy_violation with the mechanism label."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    app = FastAPI()
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_BWRAP, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": "cannot create /home/u/out.txt: Read-only file system", "exit_code": 2},
        workspace_id="ws1",
        state=active,
        started_at=1000.0,
    )
    assert len(out) == 1
    viol = out[0]
    assert viol.kind == v.VIOLATION_PREVENTED
    assert viol.mechanism == sandbox.MECHANISM_SRT_BWRAP
    assert viol.errno_name == "EROFS"
    assert viol.path == "/home/u/out.txt"
    assert v.policy_violations(app)[0]["kind"] == v.VIOLATION_PREVENTED


def test_policy_violation_event_is_trace_only() -> None:
    """``artifact.policy_violation`` is durable-only — never on the SSE wire, even on 'failed'."""
    from clio_agent.gact.semantic_events import SSE_TRACE_ONLY_EVENT_TYPES, event_reaches_ui

    assert "artifact.policy_violation" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("artifact.policy_violation") is False
    assert event_reaches_ui("artifact.policy_violation", status="failed") is False


def test_probe_sandbox_active_is_ready() -> None:
    """An active fence renders READY with the mechanism + network label."""
    from clio_agent.runtime.status import IntegrationState

    state = _srt_state()
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.READY
    assert "srt_bwrap" in row.summary
    assert "write-fence" in (row.capabilities or [])
