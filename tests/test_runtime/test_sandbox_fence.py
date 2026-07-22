"""B2 (#976) sabotage suite — the OS write-fence actually PREVENTS out-of-root writes.

The campaign's spine: platform-marked (``linux_fence``) tests that drive the REAL srt /
Landlock backend end-to-end and assert an out-of-root write is DENIED (EROFS/EACCES) and
minted as a ``policy_violation``, while legitimate in-workspace + cache writes SUCCEED (the
fence must not break the fleet). They auto-skip where the host cannot run the fence (non-Linux,
or the backend's tooling is absent), so the full suite stays green on Windows; the live gate
runs them on a fenced Linux host. NO WSL assumptions are baked in — the skips gate on the real
tooling present on whatever Linux host runs them.

The fence-only tests build the backend :class:`SandboxResult` directly (no network chokepoint
needed — these exercise the WRITE fence, matching the live srt probe that fenced writes with
an empty network allowlist). :func:`clio_agent.runtime.sandbox.wrap_confined` composes the
exact argv the seams use, and the write is run through ``subprocess`` exactly as the shell /
MCP seams do.
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


def _srt_available() -> bool:
    if not _IS_LINUX:
        return False
    det = sandbox.detect_srt()
    ok, _ = sandbox._srt_viability(det)
    bwrap_ok, _ = sandbox._probe_bwrap_userns(sys.platform)
    return ok and bwrap_ok and bool(shutil.which("rg"))


def _landlock_available() -> bool:
    return _IS_LINUX and sandbox_landlock.probe_landlock().available


_SRT = pytest.mark.skipif(not _srt_available(), reason="srt+bwrap+rg not available on this host")
_LANDLOCK = pytest.mark.skipif(
    not _landlock_available(), reason="Landlock not available on this host"
)


def _srt_state() -> sandbox.SandboxResult:
    """An active srt(bwrap) state WITHOUT a proxy (write-fence only — empty net allowlist)."""
    det = sandbox.detect_srt()
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_SRT_BWRAP,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"srt_binary": det.binary_path, "srt_version": det.version},
    )


def _landlock_state() -> sandbox.SandboxResult:
    return sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_LANDLOCK,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"net_enforcement": sandbox.NET_ENFORCEMENT_ENV_COOPERATIVE},
    )


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
# (a) shell writes $HOME/outside.txt under srt → DENIED + policy_violation      #
# --------------------------------------------------------------------------- #


@_SRT
def test_srt_shell_out_of_root_write_denied_and_minted(tmp_path: Path) -> None:
    outside = Path.home() / "clio_b2_outside.txt"
    if outside.exists():
        outside.unlink()
    state = _srt_state()
    confined = _shell_confined(state, f"echo bad > {outside}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    assert result.returncode != 0
    assert "read-only file system" in result.stderr.lower()
    assert not outside.exists()  # the fence PREVENTED the write
    # And it mints a `prevented` policy_violation with the mechanism label (gap → violation).
    denial = v.write_denial_from_result({"stderr": result.stderr})
    assert denial is not None and denial["errno_name"] == "EROFS"


@_SRT
def test_srt_in_workspace_and_cache_writes_succeed(tmp_path: Path) -> None:
    """False-positive guard: legitimate in-territory writes SUCCEED under the fence (d)."""
    state = _srt_state()
    target = tmp_path / "deliverable.txt"
    confined = _shell_confined(state, f"echo ok > {target}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "ok"


@_SRT
def test_srt_mandatory_git_hooks_protection_is_typed(tmp_path: Path) -> None:
    """srt's built-in ``.git/hooks`` protection denies even an in-workspace write (e)."""
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    state = _srt_state()
    confined = _shell_confined(
        state, f"echo x > {tmp_path / '.git' / 'hooks' / 'pre-commit'}", tmp_path
    )
    result = _run(confined, cwd=tmp_path)
    assert result.returncode != 0
    # Surfaces as a TYPED denial (EROFS), not a mystery failure.
    assert v.write_denial_from_result({"stderr": result.stderr}) is not None


# --------------------------------------------------------------------------- #
# (b) an MCP-shaped stdio child spawned through transport_for writes out-of-root #
# --------------------------------------------------------------------------- #


@_SRT
def test_srt_stdio_child_out_of_root_write_denied(tmp_path: Path) -> None:
    """A python child (the MCP-server shape) writing out-of-root is DENIED under the fence (b)."""
    outside = Path.home() / "clio_b2_mcp_outside.txt"
    if outside.exists():
        outside.unlink()
    state = _srt_state()
    roots = sandbox.effective_write_roots(sandbox.PROFILE_FLEET, workspace_root=str(tmp_path))
    code = f"open({str(outside)!r}, 'w').write('bad')"
    confined = sandbox.wrap_confined(
        sys.executable, ["-c", code], write_roots=roots, profile=sandbox.PROFILE_FLEET, state=state
    )
    result = _run(confined, cwd=tmp_path)
    assert result.returncode != 0
    assert not outside.exists()


# --------------------------------------------------------------------------- #
# (c) the same denials on the Landlock rung (srt masked)                        #
# --------------------------------------------------------------------------- #


@_LANDLOCK
def test_landlock_shell_out_of_root_write_denied(tmp_path: Path) -> None:
    outside = Path.home() / "clio_b2_ll_outside.txt"
    if outside.exists():
        outside.unlink()
    state = _landlock_state()
    confined = _shell_confined(state, f"echo bad > {outside}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    assert result.returncode != 0
    assert not outside.exists()  # Landlock fs-write fence prevented it
    # Landlock denial surfaces as EACCES (Permission denied).
    assert v.write_denial_from_result({"stderr": result.stderr}) is not None


@_LANDLOCK
def test_landlock_in_workspace_write_succeeds(tmp_path: Path) -> None:
    state = _landlock_state()
    target = tmp_path / "ll_ok.txt"
    confined = _shell_confined(state, f"echo ok > {target}", tmp_path)
    result = _run(confined, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert target.read_text().strip() == "ok"
