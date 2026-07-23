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


@pytest.fixture(autouse=True)
def _stub_egress_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """B4: ``wrap_confined`` opens a per-child egress channel whose port is the srt
    ``httpProxyPort``. Stub it deterministically (to the shared port these B2 tests already
    assert) so the composition tests stay side-effect-free (no real loopback listener) and
    port-stable. Per-child attribution itself is covered in ``test_sandbox_b4.py``.
    """
    from clio_agent.runtime import net_chokepoint

    monkeypatch.setattr(
        net_chokepoint,
        "open_child_channel",
        lambda cid, *, mechanism="", workspace_root="": 40000,
    )


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
    # On win32 the ladder consults windows_sandbox_state(), which reads the REAL provisioned
    # marker — inject an UNPROVISIONED verdict so the matrix stays host-independent (this row
    # is the unprovisioned floor case; a provisioned box would otherwise resolve srt_windows).
    win_state = None
    env = {"CLIO_SANDBOX_ENABLED": "true" if enabled else "false"}
    if platform == "win32":
        from clio_agent.runtime import sandbox_provision as swp  # noqa: PLC0415

        # pin srt: win32's default backend is now codex (B-codex-4) — this row tests the srt ladder.
        env["CLIO_SANDBOX_BACKEND"] = "srt"
        win_state = swp.WindowsSandboxState(
            status=swp.STATUS_UNPROVISIONED,
            reason=sandbox.REASON_WINDOWS_UNPROVISIONED,
            srt=det,
            detail="test: injected unprovisioned",
            next_action="run clio sandbox setup",
        )
    result = sandbox._resolve_backend(
        env=env,
        platform=platform,
        detection=det,
        bwrap=bwrap,
        landlock=_ll(landlock_ok),
        start_proxy=lambda: 40000,
        win_state=win_state,
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


def test_landlock_probe_syscall_present_but_not_enforceable_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version probe alone is a false positive — a real ruleset must be creatable (CI fix).

    A kernel where ``landlock_create_ruleset(NULL,0,VERSION)`` returns an ABI but Landlock is
    NOT in the active LSM (some CI/cloud kernels) would ``EOPNOTSUPP`` at ``restrict_self`` and
    127 every spawn. probe_landlock must report ``landlock_unavailable`` there, not activate a
    non-enforcing fence.
    """
    monkeypatch.setattr(sandbox_landlock, "_load_libc", lambda: object())  # dummy (host-agnostic)
    monkeypatch.setattr(sandbox_landlock, "_create_ruleset_version", lambda _libc: 3)  # ABI says 3
    monkeypatch.setattr(
        sandbox_landlock, "_can_create_ruleset", lambda _libc: False
    )  # can't enforce
    probe = sandbox_landlock.probe_landlock(platform="linux")
    assert probe.available is False
    assert probe.reason == sandbox_landlock.REASON_LANDLOCK_UNAVAILABLE
    assert probe.abi == 3  # the ABI is still reported for the doctor


def test_landlock_probe_enforceable_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version ABI>=1 AND a creatable ruleset → available (REFER tracked on ABI>=2)."""
    monkeypatch.setattr(sandbox_landlock, "_load_libc", lambda: object())  # dummy (host-agnostic)
    monkeypatch.setattr(sandbox_landlock, "_create_ruleset_version", lambda _libc: 3)
    monkeypatch.setattr(sandbox_landlock, "_can_create_ruleset", lambda _libc: True)
    probe = sandbox_landlock.probe_landlock(platform="linux")
    assert probe.available is True
    assert probe.refer_supported is True
    assert probe.reason == ""


# --------------------------------------------------------------------------- #
# gap → policy_violation: EROFS/EACCES mapping + the observer mint            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected", "path"),
    [
        # sh/coreutils denial (the spike's confirmed shape — strerror text, not the bracket).
        (
            "/bin/sh: 1: cannot create /home/u/out.txt: Read-only file system",
            "EROFS",
            "/home/u/out.txt",
        ),
        # bash redirect denial ("bash: line N: /p: Read-only file system") — the REAL shell the
        # fence tests use; its path form differs from sh's "cannot create" (CI-caught).
        (
            "/usr/bin/bash: line 1: /home/u/out.txt: Read-only file system",
            "EROFS",
            "/home/u/out.txt",
        ),
        ("/usr/bin/bash: line 1: /home/u/x.txt: Permission denied", "EACCES", "/home/u/x.txt"),
        # Python OSError bracket form.
        (f"[Errno {errno.EROFS}] Read-only file system: '/etc/x'", "EROFS", "/etc/x"),
        (f"[Errno {errno.EACCES}] Permission denied: '/root/y'", "EACCES", "/root/y"),
    ],
)
def test_write_denial_mapping_catches_erofs_and_eacces(text: str, expected: str, path: str) -> None:
    from clio_agent.gact.artifacts.violations import write_denial_from_result

    denial = write_denial_from_result({"stderr": text, "exit_code": 1})
    assert denial is not None
    assert denial["errno_name"] == expected
    assert denial["path"] == path  # the out-of-root path is extracted for attribution


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


# --------------------------------------------------------------------------- #
# F6 — violation errno-signal PRECISION: only an out-of-root, non-empty path    #
# mints; in-root / read / path-less denials are typed skips (no false mint).    #
# --------------------------------------------------------------------------- #


def _active_srt() -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_BWRAP, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )


# The errno signal fires only under a live srt/Landlock fence, i.e. on Linux/macOS, so the
# denial messages carry POSIX paths — the fixtures use POSIX roots to mirror that reality.
@pytest.mark.parametrize(
    ("stderr", "mints"),
    [
        # (a) read EACCES on a file (cat a 0600 file) — no create/quoted path → no extract → NO mint.
        ("cat: /ws/secret.txt: Permission denied", False),
        # (b) bare SSH/publickey 'Permission denied' with no extractable path → NO mint.
        ("git@github.com: Permission denied (publickey).", False),
        # (c) IN-ROOT EROFS (srt mandatory .git/hooks protection) → path in-root → NO mint.
        ("cannot create /ws/.git/hooks/pre-commit: Read-only file system", False),
        # (d) genuine OUT-OF-ROOT EROFS write denial with a path → MINT (prevented).
        ("cannot create /totally/outside/escaped.txt: Read-only file system", True),
    ],
)
def test_errno_signal_precision_battery(
    stderr: str, mints: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precision over recall (#966.10): only a proven out-of-root write mints (F2/F4/F6)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (Path("/ws"),))
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "s",
        tool_name="bash",
        args={},
        call_id="c",
        result={"stderr": stderr, "exit_code": 1},
        workspace_id="ws",
        state=_active_srt(),
        started_at=1000.0,
    )
    assert bool(out) is mints
    assert (len(v.policy_violations(app)) == 1) is mints
    if mints:
        assert out[0].kind == v.VIOLATION_PREVENTED
        assert out[0].path == "/totally/outside/escaped.txt"


# --------------------------------------------------------------------------- #
# F5 — no call window never upgrades a present out-of-root file to 'detected'.  #
# --------------------------------------------------------------------------- #


def test_designated_no_window_is_never_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present out-of-root designated file with started_at=None is NOT a 'detected' escape."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "out.png"
    target.write_text("x")
    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (ws,))
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.designation.grounded_output_paths",
        lambda _a: {"out": str(target)},
    )
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.designation.result_declared_paths", lambda _r: {}
    )
    app = FastAPI()
    common = {
        "tool_name": "t",
        "args": {"out": str(target)},
        "call_id": "c",
        "result": {},
        "workspace_id": "ws",
    }

    # No window → present file is UNPROVEN → skipped, never 'detected'.
    assert (
        v.observe_policy_violations(app, "s", **common, state=_active_srt(), started_at=None) == []
    )
    # A real fresh window → 'detected' (the fence was escaped).
    fresh = v.observe_policy_violations(
        app, "s", **common, state=_active_srt(), started_at=target.stat().st_mtime - 1
    )
    assert len(fresh) == 1 and fresh[0].kind == v.VIOLATION_DETECTED
    # Absent file → 'prevented' regardless of window (the fence blocked it).
    target.unlink()
    prevented = v.observe_policy_violations(
        app, "s", **common, state=_active_srt(), started_at=None
    )
    assert len(prevented) == 1 and prevented[0].kind == v.VIOLATION_PREVENTED


# --------------------------------------------------------------------------- #
# F9 — concurrent settings writes: distinct territory -> distinct files, no torn #
# read, no leftover .tmp.                                                        #
# --------------------------------------------------------------------------- #


def test_concurrent_settings_writes_no_clobber(tmp_path: Path) -> None:
    """Two threads with DIFFERENT roots land in DIFFERENT digest files, each intact (F9)."""
    import json
    import threading

    cfg_a = sandbox_srt.synthesize_srt_config([str(tmp_path / "a")], http_proxy_port=1)
    cfg_b = sandbox_srt.synthesize_srt_config([str(tmp_path / "b")], http_proxy_port=2)
    paths: dict[str, Path] = {}
    barrier = threading.Barrier(2)

    def _write(name: str, cfg: dict) -> None:
        p = sandbox_srt.settings_path_for("fleet", config=cfg, cache_dir=tmp_path)
        barrier.wait()
        for _ in range(20):
            sandbox_srt.write_settings_file(cfg, p)
        paths[name] = p

    threads = [
        threading.Thread(target=_write, args=("a", cfg_a)),
        threading.Thread(target=_write, args=("b", cfg_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert paths["a"] != paths["b"]  # distinct digests → distinct files, no clobber
    assert json.loads(paths["a"].read_text(encoding="utf-8")) == cfg_a
    assert json.loads(paths["b"].read_text(encoding="utf-8")) == cfg_b
    assert list((tmp_path / sandbox_srt.SRT_SETTINGS_DIRNAME).glob("*.tmp")) == []


def test_settings_dir_pruned_to_keep_bound(tmp_path: Path) -> None:
    """The content-addressed settings dir is bounded — old files are pruned (F3, no leak)."""
    keep = sandbox_srt.SRT_SETTINGS_KEEP
    for i in range(keep + 10):
        cfg = sandbox_srt.synthesize_srt_config([str(tmp_path / f"ws{i}")], http_proxy_port=i + 1)
        p = sandbox_srt.settings_path_for("fleet", config=cfg, cache_dir=tmp_path)
        sandbox_srt.write_settings_file(cfg, p)
    remaining = list((tmp_path / sandbox_srt.SRT_SETTINGS_DIRNAME).glob("*.json"))
    assert len(remaining) <= keep


# --------------------------------------------------------------------------- #
# F8 — net_chokepoint: real socket start, CONNECT round-trip, typed degrade,    #
# parse, idempotency, shutdown.                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("CONNECT example.com:443 HTTP/1.1", ("example.com", 443)),
        ("connect host:80 HTTP/1.0", ("host", 80)),
        ("GET http://x/ HTTP/1.1", None),  # non-CONNECT
        ("CONNECT hostonly HTTP/1.1", None),  # missing port
        ("CONNECT host:notaport HTTP/1.1", None),  # non-numeric port
        ("CONNECT host:99999 HTTP/1.1", None),  # out of range
    ],
)
def test_parse_connect_target(line: str, expected) -> None:
    from clio_agent.runtime.net_chokepoint import _parse_connect_target

    assert _parse_connect_target((line + "\r\n\r\n").encode()) == expected


def test_chokepoint_connect_round_trip() -> None:
    """A real CONNECT tunnel through the chokepoint reaches an upstream TCP echo server (F8)."""
    import socket
    import threading

    from clio_agent.runtime.net_chokepoint import Chokepoint

    # Upstream: a one-shot TCP echo server on loopback.
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    up_port = upstream.getsockname()[1]

    def _echo() -> None:
        conn, _ = upstream.accept()
        with conn:
            data = conn.recv(64)
            conn.sendall(data)

    threading.Thread(target=_echo, daemon=True).start()

    cp = Chokepoint().start()
    try:
        assert cp.port > 0
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        head = client.recv(128)
        assert b"200" in head  # tunnel established
        client.sendall(b"ping")
        assert client.recv(16) == b"ping"  # round-trip through the passthrough proxy
        client.close()
    finally:
        cp.stop()
        upstream.close()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # (method, origin-path, host, port) rewritten from absolute form.
        (
            "GET http://host:8003/search?q=x HTTP/1.1",
            ("host", 8003, b"GET /search?q=x HTTP/1.1\r\n"),
        ),
        ("POST http://ndp.example/api HTTP/1.1", ("ndp.example", 80, b"POST /api HTTP/1.1\r\n")),
        ("GET http://host HTTP/1.1", ("host", 80, b"GET / HTTP/1.1\r\n")),
        ("CONNECT host:443 HTTP/1.1", None),  # a CONNECT is not absolute-form
        ("GET https://host/x HTTP/1.1", None),  # https targets tunnel via CONNECT, not here
        ("GET /already/origin HTTP/1.1", None),  # already origin-form (not proxied absolute)
    ],
)
def test_parse_absolute_form(line: str, expected) -> None:
    from clio_agent.runtime.net_chokepoint import _parse_absolute_form

    got = _parse_absolute_form((line + "\r\nHost: h\r\n\r\n").encode())
    if expected is None:
        assert got is None
    else:
        host, port, head_prefix = expected
        assert got is not None
        assert (got[0], got[1]) == (host, port)
        # The rewritten head is origin-form + preserves the trailing headers verbatim.
        assert got[2].startswith(head_prefix)
        assert got[2].endswith(b"Host: h\r\n\r\n")


def test_chokepoint_plain_http_forward_round_trip() -> None:
    """An absolute-form plain-HTTP request forwards through the chokepoint to the origin (F-gate).

    The fleet regression the B2 live gate caught: a plain-HTTP data source (NDP on :8003) is
    reached via ``GET http://host/path`` through the proxy, which a CONNECT-only proxy
    answered 501. The passthrough must rewrite to origin-form and forward transparently.
    """
    import socket
    import threading

    from clio_agent.runtime.net_chokepoint import Chokepoint

    origin = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    origin.bind(("127.0.0.1", 0))
    origin.listen(1)
    up_port = origin.getsockname()[1]
    received: dict[str, bytes] = {}

    def _serve() -> None:
        conn, _ = origin.accept()
        with conn:
            received["head"] = conn.recv(1024)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    threading.Thread(target=_serve, daemon=True).start()

    cp = Chokepoint().start()
    try:
        client = socket.create_connection(("127.0.0.1", cp.port), timeout=5)
        client.sendall(
            f"GET http://127.0.0.1:{up_port}/search?q=earthscope HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{up_port}\r\n\r\n".encode()
        )
        resp = client.recv(256)
        assert b"200 OK" in resp and resp.endswith(b"hi")
        client.close()
    finally:
        cp.stop()
        origin.close()
    # The origin saw an ORIGIN-form request line (scheme+authority stripped), Host preserved.
    assert received["head"].startswith(b"GET /search?q=earthscope HTTP/1.1\r\n")
    assert b"http://" not in received["head"].split(b"\r\n", 1)[0]


def test_chokepoint_bind_failure_is_typed() -> None:
    """A bind/listen failure raises the typed ChokepointStartError (no silent net loss, F8)."""
    import socket as _socket

    from clio_agent.runtime import net_chokepoint as nc

    class _Boom:
        def setsockopt(self, *_a):  # noqa: D401
            pass

        def bind(self, *_a):
            raise OSError("address in use")

        def close(self):
            pass

    def _fake_socket(*_a, **_k):
        return _Boom()

    orig = nc.socket.socket
    nc.socket.socket = _fake_socket  # type: ignore[assignment]
    try:
        with pytest.raises(nc.ChokepointStartError) as exc:
            nc.Chokepoint().start()
        assert exc.value.reason == nc.REASON_CHOKEPOINT_START_FAILED
    finally:
        nc.socket.socket = orig  # type: ignore[assignment]
    assert _socket.socket is not _Boom  # sanity: real socket restored


def test_install_chokepoint_idempotent_and_shutdown_clears() -> None:
    from clio_agent.runtime import net_chokepoint as nc

    nc.shutdown_chokepoint()  # clean slate
    try:
        cp1 = nc.install_chokepoint()
        cp2 = nc.install_chokepoint()
        assert cp1 is cp2  # idempotent singleton
        assert nc.current_chokepoint() is cp1
        assert nc.chokepoint_port() == cp1.port
    finally:
        nc.shutdown_chokepoint()
    assert nc.current_chokepoint() is None
    assert nc.chokepoint_port() is None
