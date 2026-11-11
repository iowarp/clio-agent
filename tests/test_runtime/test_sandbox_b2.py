"""B2 (#976): the Landlock fallback rung, the degrade ladder, and gap→policy_violation.

Host-agnostic unit coverage (the Linux-fence sabotage suite is platform-marked, in
``test_sandbox_fence.py``, and runs in the live gate). After B-codex-5 the srt backend is
deleted — Codex is the primary OS fence (its detection/ladder-gate coverage lives in
``test_sandbox_codex_ladder.py`` / ``test_sandbox_codex_provision.py``) and **Landlock** is the
Linux fallback rung when Codex is not installed. Pinned here:

* ladder selection when Codex is absent — Landlock activates on Linux, else the honest floor;
* argv composition order ``pdeathsig( fence( final-argv ) )`` on the Landlock rung;
* the Landlock shim arg split + non-Linux probe;
* the observer mint from the seam's boundary diagnosis (EROFS, EACCES, WinError 5);
* the net chokepoint (kept for the Landlock/floor egress tier): CONNECT/absolute-form parse,
  round-trips, typed bind-failure, idempotency, shutdown.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.runtime import sandbox, sandbox_landlock
from clio_agent.runtime import sandbox_codex as sc
from clio_agent.runtime.sandbox_landlock import LandlockProbe


def test_landlock_path_beneath_has_kernel_packed_layout() -> None:
    """The packed Linux ABI stays 12 bytes without implicit ctypes layout warnings."""
    from clio_agent.runtime.landlock_exec import _PathBeneathAttr

    assert ctypes.sizeof(_PathBeneathAttr) == 12
    assert _PathBeneathAttr.allowed_access.offset == 0
    assert _PathBeneathAttr.parent_fd.offset == 8


@pytest.fixture(autouse=True)
def _stub_egress_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """B4: ``wrap_confined`` opens a per-child egress channel on the Landlock tier. Stub it
    deterministically so the composition tests stay side-effect-free (no real loopback listener)
    and port-stable. Per-child attribution itself is covered in ``test_sandbox_b4.py``.
    """
    from clio_agent.runtime import net_chokepoint

    monkeypatch.setattr(
        net_chokepoint,
        "open_child_channel",
        lambda cid, *, mechanism="", workspace_root="": 40000,
    )


# --------------------------------------------------------------------------- #
# Ladder selection — Codex absent → Landlock (Linux) → floor.                   #
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


def _ll(ok: bool) -> LandlockProbe:
    return LandlockProbe(
        available=ok,
        abi=1 if ok else 0,
        refer_supported=False,
        reason="" if ok else sandbox.REASON_LANDLOCK_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    ("platform", "codex", "landlock_ok", "enabled", "mechanism", "active"),
    [
        # Linux: codex viable → codex (the primary backend).
        ("linux", _codex_ok(), True, True, sandbox.MECHANISM_CODEX, True),
        # Linux: codex absent, Landlock present → the Landlock fallback rung.
        (
            "linux",
            _codex(sc.REASON_CODEX_NOT_INSTALLED),
            True,
            True,
            sandbox.MECHANISM_LANDLOCK,
            True,
        ),
        # Linux: codex absent, no Landlock → floor none.
        (
            "linux",
            _codex(sc.REASON_CODEX_NOT_INSTALLED),
            False,
            True,
            sandbox.MECHANISM_NONE,
            False,
        ),
        # macOS: codex viable → codex.
        ("darwin", _codex_ok(), False, True, sandbox.MECHANISM_CODEX, True),
        # macOS: codex absent → floor (no Landlock rung on darwin).
        (
            "darwin",
            _codex(sc.REASON_CODEX_NOT_INSTALLED),
            True,
            True,
            sandbox.MECHANISM_NONE,
            False,
        ),
        # Disabled knob → floor regardless of a fully-capable host.
        ("linux", _codex_ok(), True, False, sandbox.MECHANISM_NONE, False),
    ],
)
def test_ladder_selection_matrix(platform, codex, landlock_ok, enabled, mechanism, active) -> None:
    """The Codex-primary / Landlock-fallback / floor decision table (host-independent)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true" if enabled else "false"},
        platform=platform,
        codex_detection=codex,
        landlock=_ll(landlock_ok),
    )
    assert result.mechanism == mechanism
    assert result.active is active
    if active:
        assert result.reason == sandbox.REASON_FENCE_ACTIVE


# --------------------------------------------------------------------------- #
# argv composition order — pdeathsig( fence( final-argv ) ) on the Landlock rung #
# --------------------------------------------------------------------------- #


def _landlock_state() -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": "env-cooperative"},
    )


def test_wrap_confined_landlock_composes_shim() -> None:
    """An active Landlock state prepends the ``landlock_exec`` shim over the roots."""
    import sys as _sys

    confined = sandbox.wrap_confined(
        "mytool",
        ["--x"],
        write_roots=[str(Path("/ws"))],
        profile=sandbox.PROFILE_SHELL,
        state=_landlock_state(),
    )
    assert confined.command == _sys.executable
    assert confined.args[:3] == ["-m", "clio_agent.runtime.landlock_exec", str(Path("/ws"))]
    assert confined.args[-3:] == ["--", "mytool", "--x"]


def test_wrap_confined_pdeathsig_stays_outermost_over_landlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition order is ``pdeathsig( landlock( argv ) )`` — pdeathsig OUTERMOST (owner #974.5)."""
    from clio_agent.tools import mcp_config

    # A global sys.platform patch also makes Windows test paths look relative.
    monkeypatch.setattr(mcp_config, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mcp_config.shutil, "which", lambda _n: "/usr/bin/setpriv")
    confined = sandbox.wrap_confined(
        "python",
        ["-c", "print(1)"],
        write_roots=[str(Path("/ws"))],
        profile=sandbox.PROFILE_FLEET,
        pdeathsig=True,
        state=_landlock_state(),
    )
    # setpriv (pdeathsig) is outermost, then the landlock shim, then the real argv.
    assert confined.command == "/usr/bin/setpriv"
    assert confined.args[:3] == ["--pdeathsig", "SIGKILL", "--"]
    assert "clio_agent.runtime.landlock_exec" in confined.args
    assert confined.args[-3:] == ["python", "-c", "print(1)"]


def test_wrap_confined_bad_landlock_prefix_raises_not_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fence that cannot compose RAISES (typed) — never a silent unconfined spawn."""

    def _boom(*_a, **_k):
        raise ValueError("shim prefix failed")

    monkeypatch.setattr(sandbox_landlock, "landlock_shim_prefix", _boom)
    with pytest.raises(sandbox.SandboxCompositionError):
        sandbox.wrap_confined(
            "python",
            [],
            write_roots=["/ws"],
            profile=sandbox.PROFILE_FLEET,
            state=_landlock_state(),
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
# gap → policy_violation: the observer mint reads the seam's boundary diagnosis  #
# (the denial table itself is pinned in test_sandbox_diagnosis.py, #1905).       #
# --------------------------------------------------------------------------- #


def _boundary(stderr: str, roots: tuple[Path, ...], mechanism: str = "landlock") -> dict | None:
    """The ``boundary`` a confined seam attaches for ``stderr`` under ``roots``."""
    from clio_agent.runtime.sandbox_diagnosis import diagnose_boundary

    diagnosis = diagnose_boundary(
        exit_code=1,
        stderr=stderr,
        command="",
        cwd=roots[0],
        write_roots=roots,
        mechanism=mechanism,
    )
    return diagnosis.to_payload() if diagnosis is not None else None


def test_observe_policy_violations_noop_on_floor(tmp_path: Path) -> None:
    """The floor never mints a violation (its out-of-root write is an honest gap)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.violations import observe_policy_violations, policy_violations

    app = FastAPI()
    floor = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE, active=False, reason=sc.REASON_CODEX_NOT_INSTALLED
    )
    stderr = f"cannot create {tmp_path}/x: Read-only file system"
    out = observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": stderr, "boundary": _boundary(stderr, (tmp_path / "ws",))},
        workspace_id="",
        state=floor,
    )
    assert out == []
    assert policy_violations(app) == []


def test_observe_policy_violations_mints_prevented_when_fenced(tmp_path: Path) -> None:
    """A fenced result's boundary mints a ``prevented`` policy_violation with its mechanism,
    signature and message (one diagnosis, read by both lanes)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    app = FastAPI()
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    denied = tmp_path / "outside" / "out.txt"
    stderr = f"cannot create {denied}: Read-only file system"
    boundary = _boundary(stderr, (tmp_path / "ws",))
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": stderr, "exit_code": 2, "boundary": boundary},
        workspace_id="ws1",
        state=active,
        started_at=1000.0,
    )
    assert len(out) == 1
    viol = out[0]
    assert viol.kind == v.VIOLATION_PREVENTED
    assert viol.mechanism == sandbox.MECHANISM_LANDLOCK
    assert viol.errno_name == "EROFS"
    assert viol.path == str(denied)
    assert viol.next_action == boundary["message"]
    assert v.policy_violations(app)[0]["kind"] == v.VIOLATION_PREVENTED


def test_an_unfenced_tools_denial_text_is_not_a_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-process tool (no seam boundary, no confined child served it) is not fenced:
    its denial text is never parsed into a violation. SABOTAGE: diagnose every failed
    result in ``_served_call_boundary`` (drop the serving-child check) -> red."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (tmp_path / "ws",))
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.minting._workspace_root", lambda _app, _ws: tmp_path / "ws"
    )

    out = v.observe_policy_violations(
        FastAPI(),
        "sess",
        tool_name="fs_write_file",
        args={},
        call_id="c1",
        result={
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": (f"[Errno 13] Permission denied: '{tmp_path}/outside/x.txt'"),
                }
            ],
        },
        workspace_id="",
        state=_active_fence(),
    )
    assert out == []


def test_a_confined_mcp_servers_denial_is_diagnosed_and_reaches_its_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdio MCP server cannot attach the field: the observer diagnoses its failed call
    against the recomputed fleet roots, hedged to ``possible``, mints the violation and
    publishes the diagnosis for that call's model observation.
    SABOTAGE: drop ``publish_call_boundary`` in ``_served_call_boundary`` -> red."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v
    from clio_agent.runtime.sandbox_diagnosis import POSSIBLE, take_call_boundary

    workspace = tmp_path / "ws"
    workspace.mkdir()
    denied = tmp_path / "outside" / "x.txt"
    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: (workspace,))
    monkeypatch.setattr(
        "clio_agent.gact.artifacts.minting._workspace_root", lambda _app, _ws: workspace
    )
    app = FastAPI()
    app.state.net_serving_child_by_call = {"c9": "child-1"}
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="files_write",
        args={},
        call_id="c9",
        result={
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Error executing tool write: [Errno 13] Permission denied: '{denied}'"
                    ),
                }
            ],
        },
        workspace_id="ws1",
        state=_active_fence(),
        started_at=1000.0,
    )
    assert [viol.path for viol in out] == [str(denied)]
    published = take_call_boundary("c9")
    assert published is not None and published.confidence == POSSIBLE
    assert published.request_roots == (str(tmp_path),)
    assert out[0].next_action == published.message


def test_policy_violation_event_is_trace_only() -> None:
    """``artifact.policy_violation`` is durable-only — never on the SSE wire, even on 'failed'."""
    from clio_agent.gact.semantic_events import SSE_TRACE_ONLY_EVENT_TYPES, event_reaches_ui

    assert "artifact.policy_violation" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("artifact.policy_violation") is False
    assert event_reaches_ui("artifact.policy_violation", status="failed") is False


def test_probe_sandbox_active_is_ready() -> None:
    """An active fence renders READY with the mechanism + network label."""
    from clio_agent.runtime.status import IntegrationState

    state = _landlock_state()
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.READY
    assert "landlock" in row.summary
    assert "write-fence" in (row.capabilities or [])


# --------------------------------------------------------------------------- #
# F6 — violation errno-signal PRECISION: only an out-of-root, non-empty path    #
# mints; in-root / read / path-less denials are typed skips (no false mint).    #
# --------------------------------------------------------------------------- #


def _active_fence() -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )


@pytest.mark.parametrize(
    ("template", "mints"),
    [
        # (a) read EACCES on an in-root file (cat a 0600 file) → no diagnosis → NO mint.
        ("cat: {ws}/secret.txt: Permission denied", False),
        # (b) bare SSH/publickey 'Permission denied': a path-less, possible diagnosis → NO mint.
        ("git@github.com: Permission denied (publickey).", False),
        # (c) IN-ROOT EROFS (mandatory .git/hooks protection) → path in-root → NO mint.
        ("cannot create {ws}/.git/hooks/pre-commit: Read-only file system", False),
        # (d) genuine OUT-OF-ROOT EROFS write denial with a path → MINT (prevented).
        ("cannot create {out}/escaped.txt: Read-only file system", True),
    ],
)
def test_errno_signal_precision_battery(template: str, mints: bool, tmp_path: Path) -> None:
    """Precision over recall (#966.10): only a proven out-of-root write mints (F2/F4/F6)."""
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    workspace, outside = tmp_path / "ws", tmp_path / "totally" / "outside"
    workspace.mkdir()
    stderr = template.format(ws=workspace, out=outside)
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "s",
        tool_name="bash",
        args={},
        call_id="c",
        result={"stderr": stderr, "exit_code": 1, "boundary": _boundary(stderr, (workspace,))},
        workspace_id="ws",
        state=_active_fence(),
        started_at=1000.0,
    )
    assert bool(out) is mints
    assert (len(v.policy_violations(app)) == 1) is mints
    if mints:
        assert out[0].kind == v.VIOLATION_PREVENTED
        assert out[0].path == str(outside / "escaped.txt")


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
        v.observe_policy_violations(app, "s", **common, state=_active_fence(), started_at=None)
        == []
    )
    # A real fresh window → 'detected' (the fence was escaped).
    fresh = v.observe_policy_violations(
        app, "s", **common, state=_active_fence(), started_at=target.stat().st_mtime - 1
    )
    assert len(fresh) == 1 and fresh[0].kind == v.VIOLATION_DETECTED
    # Absent file → 'prevented' regardless of window (the fence blocked it).
    target.unlink()
    prevented = v.observe_policy_violations(
        app, "s", **common, state=_active_fence(), started_at=None
    )
    assert len(prevented) == 1 and prevented[0].kind == v.VIOLATION_PREVENTED


# --------------------------------------------------------------------------- #
# WinError 5 → policy_violation (the EROFS/EACCES path, extended for Windows).   #
# --------------------------------------------------------------------------- #


def test_winerror5_mints_prevented_when_fenced(tmp_path: Path) -> None:
    """A fenced out-of-root WinError-5 mints a prevented violation (codex mechanism).

    Uses REAL ``tmp_path`` roots so containment is evaluated with the running OS's own
    Path flavor — portable on Linux CI and Windows alike.
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside" / "escaped.txt"
    active = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX, active=True, reason=sandbox.REASON_FENCE_ACTIVE
    )
    stderr = f"[WinError 5] Access is denied: '{outside}'"
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": stderr, "boundary": _boundary(stderr, (ws,), "codex")},
        workspace_id="ws1",
        state=active,
        started_at=1000.0,
    )
    assert len(out) == 1
    assert out[0].kind == v.VIOLATION_PREVENTED
    assert out[0].mechanism == sandbox.MECHANISM_CODEX
    assert out[0].errno_name == "ERROR_ACCESS_DENIED"
    assert out[0].path == str(outside)


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


def test_a_call_with_nothing_to_place_never_resolves_the_write_territory(monkeypatch) -> None:
    """A fenced call with no denial and no output path skips the roots (the read-only burst).

    Resolving the effective write roots walks every root through the filesystem; on a burst
    of read-only MCP calls it was most of the tool observer's own time, for no possible
    violation.
    """
    from fastapi import FastAPI

    from clio_agent.gact.artifacts import violations as v

    def refuse(_app, _ws):
        raise AssertionError("the write territory was resolved for a call with nothing to place")

    monkeypatch.setattr(v, "_effective_roots", refuse)
    out = v.observe_policy_violations(
        FastAPI(),
        "sess",
        tool_name="get_record",
        args={"record_id": "R1"},
        call_id="c1",
        result={"record_id": "R1", "value": "ok"},
        workspace_id="",
        state=_active_fence(),
    )
    assert out == []
