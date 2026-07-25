"""B-codex (#974): the Codex backend wired into the sandbox ladder (resolve/compose/doctor).

Host-agnostic unit coverage — no real codex/node spawn. Every probe is INJECTED
(``codex_detection`` / ``landlock`` on :func:`sandbox._resolve_backend`, explicit ``platform``
strings, a ``tmp_path`` codex home). After B-codex-5 Codex is the SOLE OS-fence backend (srt is
deleted); the Linux fallback is Landlock, elsewhere the honest floor. Pinned:

* selection matrix — viable codex → MECHANISM_CODEX active (all platforms; win32 additionally
  gates on provisioning); absent/old codex → typed floor (Landlock on Linux, else the codex reason);
* :func:`sandbox_codex.compose_codex_spawn` argv shape, the win32-only elevated layer gate
  (asserted via the written layer file), and the empty-write-roots typed raise;
* the doctor row — a codex-active result is READY; a codex floor reason is DEGRADED with the
  install-codex next_action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.runtime import sandbox
from clio_agent.runtime import sandbox_codex as sc
from clio_agent.runtime.sandbox_landlock import LandlockProbe
from clio_agent.runtime.status import IntegrationState

# --------------------------------------------------------------------------- #
# Injected detection fakes — never a real probe.                               #
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


# --------------------------------------------------------------------------- #
# Selection matrix — Codex is the primary backend on ALL platforms.             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_codex_viable_activates_codex(platform: str) -> None:
    """A viable codex detection → MECHANISM_CODEX active on every platform.

    On win32 (B-codex-3, #1026) activation ADDITIONALLY requires a provisioned +
    enforcement-verified probe — injected here — so the win32 rung is honest; off-win32 codex
    activates from ``detect_codex`` viability alone (no gate).
    """
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true"},
        platform=platform,
        codex_detection=_codex_ok(),
        # win32 gate: a provisioned + enforcement-verified probe (ignored off-win32).
        codex_provisioned_probe=lambda: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
    )
    assert result.mechanism == sandbox.MECHANISM_CODEX
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["codex_binary"] == "/usr/bin/codex"
    assert result.details["codex_version"] == "0.145.0"
    # Network egress is RECORDED via clio's upstream chokepoint (Recipe A) → proxy-enforced label.
    assert result.details["net_enforcement"] == sandbox.NET_ENFORCEMENT_PROXY


@pytest.mark.parametrize(
    ("cdet", "reason"),
    [
        (_codex(sc.REASON_CODEX_NOT_INSTALLED), sc.REASON_CODEX_NOT_INSTALLED),
        (
            _codex(sc.REASON_CODEX_VERSION_UNSUPPORTED, installed=True, version="0.100.0"),
            sc.REASON_CODEX_VERSION_UNSUPPORTED,
        ),
    ],
)
def test_codex_not_viable_floors_typed(cdet: sc.CodexDetection, reason: str) -> None:
    """codex absent / below the floor on win32 → the honest floor with a typed reason."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true"},
        platform="win32",
        codex_detection=cdet,
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == reason


def test_codex_absent_on_linux_falls_to_landlock() -> None:
    """codex absent on Linux → the Landlock fallback rung (codex is not the only Linux option)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true"},
        platform="linux",
        codex_detection=_codex(sc.REASON_CODEX_NOT_INSTALLED),
        landlock=_ll(True),
    )
    assert result.mechanism == sandbox.MECHANISM_LANDLOCK
    assert result.active is True
    assert result.details["codex_skip_reason"] == sc.REASON_CODEX_NOT_INSTALLED


def test_codex_still_honors_disabled_knob() -> None:
    """The ladder never runs when confinement is disabled — the floor stays REASON_DISABLED."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "false"},
        platform="linux",
        codex_detection=_codex_ok(),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_DISABLED


# --------------------------------------------------------------------------- #
# compose_codex_spawn — argv shape, elevated gate, empty-roots raise.           #
# --------------------------------------------------------------------------- #


def test_compose_codex_spawn_argv_shape(tmp_path: Path) -> None:
    """The composed argv is ``[binary, sandbox, -p <layer>, --permission-profile clio, -C r0, --, …]``."""
    cmd, args = sc.compose_codex_spawn(
        ["D:\\ws"],
        "python",
        ["-c", "print(1)"],
        binary="C:\\tools\\codex.cmd",
        platform="win32",
        codex_home=tmp_path,
    )
    assert cmd == "C:\\tools\\codex.cmd"
    assert args[0] == "sandbox"
    assert args[1] == "-p"
    layer = args[2]
    assert layer.startswith("clio-sb-")  # the content-addressed -p layer
    assert args[3:8] == ["--permission-profile", "clio", "-C", "D:\\ws", "--"]
    assert args[8:] == ["python", "-c", "print(1)"]  # the wrapped child argv, verbatim


def test_compose_codex_spawn_elevated_layer_only_on_win32(tmp_path: Path) -> None:
    """The written -p layer carries ``[windows] sandbox = "elevated"`` ONLY on win32."""
    win_home = tmp_path / "win"
    nix_home = tmp_path / "nix"
    sc.compose_codex_spawn(
        ["D:\\ws"], "cmd", [], binary="codex.cmd", platform="win32", codex_home=win_home
    )
    sc.compose_codex_spawn(["/ws"], "sh", [], binary="codex", platform="linux", codex_home=nix_home)
    win_layer = next(win_home.glob(sc.CODEX_LAYER_GLOB)).read_text(encoding="utf-8")
    nix_layer = next(nix_home.glob(sc.CODEX_LAYER_GLOB)).read_text(encoding="utf-8")
    assert "[windows]" in win_layer and 'sandbox = "elevated"' in win_layer
    assert "[windows]" not in nix_layer  # off-win32 the elevated gate is omitted


def test_compose_codex_spawn_empty_roots_raises_typed(tmp_path: Path) -> None:
    """No write territory → a typed CodexSpawnError (an empty fence would confine nothing)."""
    with pytest.raises(sc.CodexSpawnError):
        sc.compose_codex_spawn([], "python", [], binary="codex", codex_home=tmp_path)


def test_compose_codex_spawn_wraps_typed_in_composition_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Via the ladder's _compose_fence_prefix, an empty fence is a typed SandboxCompositionError."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"codex_binary": "codex"},
    )
    with pytest.raises(sandbox.SandboxCompositionError):
        sandbox._compose_fence_prefix(state, sandbox.PROFILE_FLEET, "python", [], write_roots=[])


def test_compose_fence_prefix_codex_composes_layer(tmp_path: Path) -> None:
    """An active codex state composes the codex prefix from the state's recorded binary."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={"codex_binary": "codex"},
    )
    import os

    os.environ["CODEX_HOME"] = str(tmp_path)
    try:
        cmd, args = sandbox._compose_fence_prefix(
            state, sandbox.PROFILE_FLEET, "python", ["-V"], write_roots=[str(tmp_path)]
        )
    finally:
        os.environ.pop("CODEX_HOME", None)
    assert cmd == "codex"
    assert args[0] == "sandbox"
    assert args[-3:] == ["--", "python", "-V"]


# --------------------------------------------------------------------------- #
# Doctor row — codex-active READY, codex floor DEGRADED with install action.    #
# --------------------------------------------------------------------------- #


def test_probe_sandbox_codex_active_is_ready() -> None:
    """A codex-active result renders READY (MECHANISM_CODEX is a known OS fence)."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_CODEX,
        active=True,
        reason=sandbox.REASON_FENCE_ACTIVE,
        details={
            "codex_binary": "/usr/bin/codex",
            "net_enforcement": sandbox.NET_ENFORCEMENT_ENV_COOPERATIVE,
        },
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.READY
    assert "codex" in row.summary
    assert "write-fence" in (row.capabilities or [])


@pytest.mark.parametrize(
    "reason", [sc.REASON_CODEX_NOT_INSTALLED, sc.REASON_CODEX_VERSION_UNSUPPORTED]
)
def test_probe_sandbox_codex_floor_is_degraded_with_install_action(reason: str) -> None:
    """A codex floor reason renders DEGRADED with the guided install-codex next_action."""
    state = sandbox.SandboxResult(mechanism=sandbox.MECHANISM_NONE, active=False, reason=reason)
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.DEGRADED
    assert "npm install -g @openai/codex" in row.next_action
