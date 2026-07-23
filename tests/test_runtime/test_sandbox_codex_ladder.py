"""B-codex-2 (#974): Codex backend wired into the sandbox ladder (resolve/compose/doctor).

Host-agnostic unit coverage — no real codex/srt/node spawn. Every probe is INJECTED
(``codex_detection`` / ``detection`` on :func:`sandbox._resolve_backend`, explicit ``platform``
strings, a ``tmp_path`` codex home). Pinned:

* selection matrix — ``CLIO_SANDBOX_BACKEND=codex`` + viable codex → MECHANISM_CODEX active (all
  platforms, incl. win32 with NO provisioning gate this slice); + absent/old codex → typed floor;
  backend unset / ``"srt"`` → the existing srt/Landlock/floor resolution is UNCHANGED (codex
  ignored even when injected viable);
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


def _srt_none() -> sandbox.SrtDetection:
    """A benign 'srt absent' detection injected so the ladder never runs a real host probe."""
    return sandbox.SrtDetection(
        installed=False,
        binary_path="",
        version="",
        node_present=False,
        node_version="",
        node_ok=False,
        socat_present=False,
        reason=sandbox.REASON_SRT_NOT_INSTALLED,
    )


def _srt_ok() -> sandbox.SrtDetection:
    return sandbox.SrtDetection(
        installed=True,
        binary_path="/opt/srt",
        version="0.0.66",
        node_present=True,
        node_version="v22.0.0",
        node_ok=True,
        socat_present=True,
        reason=sandbox.REASON_SRT_DETECTED_DEFERRED,
    )


def _ll(ok: bool) -> LandlockProbe:
    return LandlockProbe(
        available=ok,
        abi=1 if ok else 0,
        refer_supported=False,
        reason="" if ok else sandbox.REASON_LANDLOCK_UNAVAILABLE,
    )


# --------------------------------------------------------------------------- #
# _sandbox_backend flag reader.                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("codex", "codex"),
        ("CODEX", "codex"),  # case-insensitive
        (" srt ", "srt"),
        ("srt", "srt"),
        ("", "srt"),  # unset → the platform default (linux → srt)
        ("bogus", "srt"),  # unrecognized → the platform default (linux → srt)
    ],
)
def test_sandbox_backend_reads_flag(raw: str, expected: str) -> None:
    # Pin platform=linux so the unset/bogus rows resolve the srt default deterministically
    # (the default is platform-aware since B-codex-4: win32 → codex, elsewhere → srt).
    assert sandbox._sandbox_backend({"CLIO_SANDBOX_BACKEND": raw}, platform="linux") == expected


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({}, "win32", "codex"),  # unset on win32 → codex (B-codex-4 platform default)
        ({"CLIO_SANDBOX_BACKEND": ""}, "win32", "codex"),  # blank on win32 → codex
        ({"CLIO_SANDBOX_BACKEND": "bogus"}, "win32", "codex"),  # unrecognized on win32 → codex
        ({"CLIO_SANDBOX_BACKEND": "srt"}, "win32", "srt"),  # explicit srt still wins on win32
        ({}, "linux", "srt"),  # unset off-win32 → srt
    ],
)
def test_sandbox_backend_default_is_platform_aware(
    env: dict[str, str], platform: str, expected: str
) -> None:
    """The default backend is codex on win32 (srt's Windows fence is broken), srt elsewhere."""
    assert sandbox._sandbox_backend(env, platform=platform) == expected


# --------------------------------------------------------------------------- #
# Selection matrix — backend=codex resolves the Codex rung on ALL platforms.    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_backend_codex_viable_activates_codex(platform: str) -> None:
    """backend=codex + a viable codex detection → MECHANISM_CODEX active on every platform.

    Codex resolves BEFORE the srt/Landlock ladder. On win32 (B-codex-3, #1026) activation
    ADDITIONALLY requires a provisioned + enforcement-verified probe — injected here — so the win32
    rung is honest; off-win32 codex activates from ``detect_codex`` viability alone (no gate).
    """
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "codex", "CLIO_SANDBOX_ENABLED": "true"},
        platform=platform,
        detection=_srt_none(),  # injected so the ladder never runs a real srt/node probe
        codex_detection=_codex_ok(),
        # win32 gate: a provisioned + enforcement-verified probe (ignored off-win32).
        codex_provisioned_probe=lambda: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
    )
    assert result.mechanism == sandbox.MECHANISM_CODEX
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["codex_binary"] == "/usr/bin/codex"
    assert result.details["codex_version"] == "0.145.0"
    # Network egress is DEFERRED — the write-fence is active with the honest deferred net label.
    assert result.details["net_enforcement"] == "codex-net-deferred"


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
def test_backend_codex_not_viable_floors_typed(cdet: sc.CodexDetection, reason: str) -> None:
    """backend=codex but codex absent / below the floor → the honest floor with a typed reason."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "codex", "CLIO_SANDBOX_ENABLED": "true"},
        platform="win32",
        detection=_srt_none(),
        codex_detection=cdet,
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == reason


def test_backend_codex_still_honors_disabled_knob() -> None:
    """The Codex path never runs when confinement is disabled — the floor stays REASON_DISABLED."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_BACKEND": "codex", "CLIO_SANDBOX_ENABLED": "false"},
        platform="linux",
        detection=_srt_none(),
        codex_detection=_codex_ok(),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.reason == sandbox.REASON_DISABLED


# --------------------------------------------------------------------------- #
# The DEFAULT srt ladder is UNCHANGED when the backend is unset / "srt".        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend_env", [{}, {"CLIO_SANDBOX_BACKEND": "srt"}])
def test_backend_srt_default_resolves_srt_ladder_unchanged(backend_env: dict[str, str]) -> None:
    """Unset / ``srt`` backend → the existing srt path resolves; the codex probe is IGNORED.

    A viable codex detection is injected to prove it is never consulted off the flag.
    """
    result = sandbox._resolve_backend(
        env={**backend_env, "CLIO_SANDBOX_ENABLED": "true"},
        platform="linux",
        detection=_srt_ok(),
        codex_detection=_codex_ok(),  # present + viable, but must be ignored on the srt backend
        bwrap=(True, ""),
        landlock=_ll(True),
        start_proxy=lambda: 40000,
    )
    assert result.mechanism == sandbox.MECHANISM_SRT_BWRAP
    assert result.active is True
    assert "codex_binary" not in result.details  # the codex rung was never taken


def test_backend_srt_default_floor_is_unchanged() -> None:
    """Unset backend + srt absent + no Landlock → the same honest floor as before (none)."""
    result = sandbox._resolve_backend(
        env={"CLIO_SANDBOX_ENABLED": "true"},
        platform="linux",
        detection=_srt_none(),
        codex_detection=_codex_ok(),
        bwrap=(False, sandbox.REASON_BWRAP_UNAVAILABLE),
        landlock=_ll(False),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False


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
