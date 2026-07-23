"""B2 (#976) sabotage suite — the OS write-fence actually PREVENTS out-of-root writes.

The campaign's spine: platform-marked (``linux_fence``) tests that drive the REAL Landlock
backend end-to-end and assert an out-of-root write is DENIED (EROFS/EACCES) and minted as a
``policy_violation``, while legitimate in-workspace + cache writes SUCCEED (the fence must not
break the fleet). They auto-skip where the host cannot run the fence (non-Linux, or Landlock
absent), so the full suite stays green on Windows; the live gate runs them on a fenced Linux
host. NO WSL assumptions are baked in — the skips gate on the real tooling present on whatever
Linux host runs them. (The srt rung was deleted in B-codex-5; Codex is the primary backend, its
own Linux fence is covered by the live gate, not this Landlock-fallback sabotage suite.)

The fence-only tests build the backend :class:`SandboxResult` directly (no network chokepoint
needed — these exercise the WRITE fence). :func:`clio_agent.runtime.sandbox.wrap_confined`
composes the exact argv the seams use, and the write is run through ``subprocess`` exactly as
the shell / MCP seams do.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.artifacts import violations as v
from clio_agent.runtime import sandbox, sandbox_landlock

pytestmark = pytest.mark.linux_fence

_IS_LINUX = sys.platform.startswith("linux")


def _landlock_available() -> bool:
    return _IS_LINUX and sandbox_landlock.probe_landlock().available


_LANDLOCK = pytest.mark.skipif(
    not _landlock_available(), reason="Landlock not available on this host"
)


def _landlock_state() -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": sandbox.NET_ENFORCEMENT_ENV_COOPERATIVE},
    )


def _require_fence_enforced(result: subprocess.CompletedProcess) -> None:
    """Backstop the capability gate: if the shim reports it could not APPLY the fence, the
    rung is not enforceable on this host despite the probe — skip with the TYPED reason rather
    than assert a silent ``[]`` (Class 2, #976 review). A real enforced denial never hits this.
    """
    from clio_agent.runtime.landlock_exec import EXIT_FENCE_FAILED

    if result.returncode == EXIT_FENCE_FAILED and "landlock_apply_failed" in (result.stderr or ""):
        pytest.skip(f"landlock rung not enforceable on this host: {result.stderr.strip()[:160]}")


def _run(confined: sandbox.ConfinedSpawn, *, cwd: Path) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **confined.env_overlay} if confined.env_overlay else None
    return subprocess.run(
        [confined.command, *confined.args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
        **confined.popen_kwargs,
    )


def _shell_confined(
    state: sandbox.SandboxResult, command: str, workspace: Path
) -> sandbox.ConfinedSpawn:
    sh = shutil.which("bash") or shutil.which("sh")
    assert sh is not None
    roots = sandbox.effective_write_roots(sandbox.PROFILE_SHELL, workspace_root=str(workspace))
    return sandbox.wrap_confined(
        sh, ["-c", command], write_roots=roots, profile=sandbox.PROFILE_SHELL, state=state
    )


# --------------------------------------------------------------------------- #
# (a) shell writes $HOME/outside.txt under the Landlock fence → DENIED + minted  #
# --------------------------------------------------------------------------- #


def _assert_mints_real_violation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: subprocess.CompletedProcess,
    *,
    mechanism: str,
    outside: Path,
) -> None:
    """F7: feed the REAL fenced stderr through the observer + assert a real PolicyViolation."""
    from fastapi import FastAPI

    roots = sandbox.effective_write_roots(sandbox.PROFILE_SHELL, workspace_root=str(tmp_path))
    monkeypatch.setattr(v, "_effective_roots", lambda _app, _ws: roots)
    app = FastAPI()
    out = v.observe_policy_violations(
        app,
        "sess",
        tool_name="bash",
        args={},
        call_id="c1",
        result={"stderr": result.stderr, "exit_code": result.returncode},
        workspace_id="ws",
        state=sandbox.SandboxResult(
            mechanism=mechanism, active=True, reason=sandbox.REASON_FENCE_ACTIVE
        ),
        started_at=1000.0,
    )
    assert len(out) == 1, out
    viol = out[0]
    assert viol.kind == v.VIOLATION_PREVENTED
    assert viol.mechanism == mechanism
    assert str(outside) in viol.path  # the extracted out-of-root path
    ledger = v.policy_violations(app)
    assert ledger and ledger[0]["kind"] == v.VIOLATION_PREVENTED


# --------------------------------------------------------------------------- #
# The write-fence denials on the Landlock rung (the Linux Codex-fallback)        #
# --------------------------------------------------------------------------- #


@_LANDLOCK
def test_landlock_shell_out_of_root_write_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = Path.home() / "clio_b2_ll_outside.txt"
    if outside.exists():
        outside.unlink()
    state = _landlock_state()
    confined = _shell_confined(state, f"echo bad > {outside}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    _require_fence_enforced(result)
    assert result.returncode != 0
    assert not outside.exists()  # Landlock fs-write fence prevented it
    # F7: the REAL Landlock EACCES stderr parses to a REAL prevented policy_violation.
    _assert_mints_real_violation(
        monkeypatch, tmp_path, result, mechanism=sandbox.MECHANISM_LANDLOCK, outside=outside
    )


@_LANDLOCK
def test_landlock_cross_dir_rename_in_root_succeeds(tmp_path: Path) -> None:
    """F1 regression: a cross-dir os.replace BETWEEN two allowed roots succeeds (REFER on ABI>=2).

    The ubiquitous stage-then-atomic-replace pattern (matplotlib/pandas): write into
    ``ws/stage``, ``os.replace`` into ``ws/out`` — both inside the workspace root. Without the
    ABI>=2 REFER grant Landlock denies this with EXDEV, a false in-territory fence break.
    """
    (tmp_path / "stage").mkdir()
    (tmp_path / "out").mkdir()
    src = tmp_path / "stage" / "f.txt"
    dst = tmp_path / "out" / "f.txt"
    state = _landlock_state()
    roots = sandbox.effective_write_roots(sandbox.PROFILE_FLEET, workspace_root=str(tmp_path))
    code = f"import os; open({str(src)!r},'w').write('x'); os.replace({str(src)!r}, {str(dst)!r})"
    confined = sandbox.wrap_confined(
        sys.executable, ["-c", code], write_roots=roots, profile=sandbox.PROFILE_FLEET, state=state
    )
    result = _run(confined, cwd=tmp_path)
    _require_fence_enforced(result)
    assert result.returncode == 0, result.stderr  # REFER lets the in-root reparent through
    assert dst.exists() and not src.exists()


@_LANDLOCK
def test_landlock_out_of_root_rename_still_denied(tmp_path: Path) -> None:
    """F1 containment: REFER is granted only beneath the roots — an out-of-root reparent fails."""
    (tmp_path / "stage").mkdir()
    src = tmp_path / "stage" / "g.txt"
    outside = Path.home() / "clio_b2_refer_escape.txt"
    if outside.exists():
        outside.unlink()
    state = _landlock_state()
    roots = sandbox.effective_write_roots(sandbox.PROFILE_FLEET, workspace_root=str(tmp_path))
    code = (
        f"import os; open({str(src)!r},'w').write('x'); os.replace({str(src)!r}, {str(outside)!r})"
    )
    confined = sandbox.wrap_confined(
        sys.executable, ["-c", code], write_roots=roots, profile=sandbox.PROFILE_FLEET, state=state
    )
    result = _run(confined, cwd=tmp_path)
    _require_fence_enforced(result)
    assert result.returncode != 0
    assert not outside.exists()  # containment preserved despite REFER


@_LANDLOCK
def test_landlock_in_workspace_write_succeeds(tmp_path: Path) -> None:
    state = _landlock_state()
    target = tmp_path / "ll_ok.txt"
    confined = _shell_confined(state, f"echo ok > {target}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    _require_fence_enforced(result)
    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "ok"


@_LANDLOCK
def test_landlock_dev_null_write_succeeds(tmp_path: Path) -> None:
    """False-positive guard: writing to /dev/null MUST succeed under the Landlock fence.

    Nearly every child redirects to ``/dev/null``; the native rung must grant it (srt/bwrap
    bind it automatically). Without the dev-node allowance the fence breaks ordinary commands.
    """
    state = _landlock_state()
    confined = _shell_confined(state, "echo discarded > /dev/null; echo lives", tmp_path)
    result = _run(confined, cwd=tmp_path)
    _require_fence_enforced(result)
    assert result.returncode == 0, result.stderr
    assert "lives" in result.stdout
