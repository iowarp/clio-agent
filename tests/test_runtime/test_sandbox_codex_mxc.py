"""MXC qualifies actual policy; launch failures and stale receipts never activate it."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.runtime import sandbox, sandbox_cli, sandbox_codex, sandbox_net
from clio_agent.runtime import sandbox_codex_mxc as mxc
from clio_agent.runtime.sandbox_codex_probe import ProbeRun


@pytest.mark.parametrize(
    "ordinary, extended",
    [
        (r"D:\Runtime\codex.exe", r"\\?\D:\Runtime\codex.exe"),
        (r"\\server\share\Runtime\codex.exe", r"\\?\UNC\server\share\Runtime\codex.exe"),
    ],
)
def test_windows_receipt_identifies_path_aliases_as_the_same_binary(
    monkeypatch: pytest.MonkeyPatch, ordinary: str, extended: str
) -> None:
    monkeypatch.setattr(mxc, "sys", SimpleNamespace(platform="win32"))
    assert mxc._binary_identity_path(Path(ordinary)) == mxc._binary_identity_path(Path(extended))
    assert mxc._binary_identity_path(Path(ordinary.upper())) == mxc._binary_identity_path(
        Path(ordinary)
    )


def test_native_build_does_not_depend_on_platform_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> str:
        pytest.fail("The synthetic platform cache cannot qualify a Windows build")

    # A 25H2 system reports its servicing family's base build here (26100); the
    # registry holds the installed build (26200).
    native = SimpleNamespace(platform_version=(10, 0, 26100))
    monkeypatch.setattr(
        mxc, "sys", SimpleNamespace(platform="win32", getwindowsversion=lambda: native)
    )
    monkeypatch.setattr(mxc.platform, "version", forbidden)
    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            HKEY_LOCAL_MACHINE=1,
            OpenKey=lambda *args: nullcontext(2),
            QueryValueEx=lambda key, name: {"UBR": (9711, 4), "CurrentBuildNumber": ("26200", 1)}[
                name
            ],
        ),
    )
    assert mxc._host_build() == "10.0.26200.9711"


def test_receipt_invalidates_when_windows_revision_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"test-only executable identity")
    monkeypatch.setattr(mxc, "_receipt_path", lambda: tmp_path / "receipt.json")
    monkeypatch.setattr(mxc, "_host_build", lambda: "10.0.26100.9550")
    monkeypatch.setattr(mxc, "sys", SimpleNamespace(platform="win32"))
    mxc.prepare_mxc(str(binary), "0.167.0", probe=lambda binary: True)
    assert mxc.mxc_ready(str(binary), version="0.167.0")
    monkeypatch.setattr(mxc, "_host_build", lambda: "10.0.26100.9551")
    assert not mxc.mxc_ready(str(binary), version="0.167.0")


def test_active_mxc_cannot_silently_downgrade_to_elevated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mxc, "mxc_ready", lambda *args, **kwargs: False)
    compose = sandbox_codex.compose_codex_spawn

    def windows_spawn(*args: Any, **kwargs: Any) -> tuple[str, list[str]]:
        return compose(*args, platform="win32", **kwargs)

    monkeypatch.setattr(sandbox_codex, "compose_codex_spawn", windows_spawn)
    state = sandbox.SandboxResult(
        mechanism="codex",
        active=True,
        reason="fence_active",
        details={
            "codex_binary": "codex.exe",
            "codex_version": "0.167.0",
            "codex": {"implementation": "mxc"},
        },
    )
    with pytest.raises(sandbox.SandboxCompositionError, match="qualification changed"):
        sandbox._compose_fence_prefix(state, sandbox.PROFILE_SHELL, "python", [], [tmp_path])


@pytest.fixture
def isolated_mxc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mxc, "sys", SimpleNamespace(platform="win32", executable="python.exe"))
    monkeypatch.setattr(mxc, "_receipt_path", lambda: tmp_path / "receipt.json")
    monkeypatch.setattr(
        mxc, "_identity", lambda binary, version="": {"binary": binary, "version": version}
    )
    return tmp_path


@pytest.mark.parametrize("verified", [True, False])
def test_setup_reuses_qualified_or_unsupported_result_until_identity_changes(
    isolated_mxc: Path, monkeypatch: pytest.MonkeyPatch, verified: bool
) -> None:
    calls: list[str] = []

    def probe(binary: str) -> bool:
        calls.append(binary)
        return verified

    first = mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe)
    assert first["status"] == ("verified" if verified else "unavailable")
    assert mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe) == first
    assert mxc.mxc_ready("codex.exe", version="0.167.0") is verified
    assert len(calls) == 1
    monkeypatch.setattr(
        mxc,
        "_identity",
        lambda binary, version="": {"binary": binary, "version": version, "build": "updated"},
    )
    assert not mxc.mxc_ready("codex.exe")
    mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"identity": []},
        {"identity": {"version": []}},
        {"identity": {"version": "0.167.0"}, "status": []},
    ],
)
def test_malformed_receipt_never_qualifies(isolated_mxc: Path, payload: Any) -> None:
    (isolated_mxc / "receipt.json").write_text(json.dumps(payload))
    assert not mxc.mxc_ready("codex.exe")


@pytest.mark.parametrize(
    "error", [OSError("missing native feature"), subprocess.TimeoutExpired("codex", 30)]
)
def test_probe_error_never_publishes_success(isolated_mxc: Path, error: Exception) -> None:
    def probe(binary: str) -> bool:
        raise error

    assert mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe)["status"] == "unavailable"
    assert not mxc.mxc_ready("codex.exe")


@pytest.mark.parametrize("initially_verified", [False, True])
def test_explicit_recovery_rechecks_native_policy(
    isolated_mxc: Path, initially_verified: bool
) -> None:
    mxc.prepare_mxc("codex.exe", "0.167.0", probe=lambda binary: initially_verified)
    mxc.prepare_mxc("codex.exe", "0.167.0", probe=lambda binary: not initially_verified)
    assert mxc.mxc_ready("codex.exe") is initially_verified
    mxc.prepare_mxc("codex.exe", "0.167.0", probe=lambda binary: not initially_verified, force=True)
    assert mxc.mxc_ready("codex.exe") is not initially_verified


@pytest.mark.parametrize("broken", ["none", "launcher", "outside", "source", *sorted(mxc._CHECKS)])
def test_proof_requires_every_positive_and_denial_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(mxc, "sys", SimpleNamespace(executable="fixture-python"))

    def run(argv: list[str], **kwargs: Any) -> ProbeRun:
        workspace = Path(argv[argv.index("-C") + 1])
        checks = dict.fromkeys(mxc._CHECKS, True)
        if broken in checks:
            checks[broken] = False
        (workspace / "inside.txt").write_text("allowed")
        (workspace / "checks.json").write_text(json.dumps(checks))
        if broken == "outside":
            (workspace.parent / "outside.txt").write_text("escaped")
        if broken == "source":
            (workspace / "source/data.txt").write_text("changed")
        assert kwargs["timeout"] == 30
        assert "--include-managed-config" in argv
        if broken == "launcher":
            return ProbeRun(1, False, "MXC launcher: rejects the config")
        return ProbeRun(0, False, "")

    monkeypatch.setattr(mxc, "run_probe", run)
    outcome = mxc._probe_mxc("codex.exe")
    assert outcome.verified is (broken == "none")
    if broken == "launcher":
        # Codex's refusal is kept, so the cause reaches the receipt and the message.
        assert outcome.reason == mxc.REASON_MXC_LAUNCH_FAILED
        assert "rejects the config" in outcome.detail
    elif broken != "none":
        assert outcome.reason == mxc.REASON_MXC_UNVERIFIED


def test_probe_timeout_is_reported_as_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(mxc, "sys", SimpleNamespace(executable="fixture-python"))
    monkeypatch.setattr(mxc, "run_probe", lambda argv, **kwargs: ProbeRun(None, True, "slow mount"))
    outcome = mxc._probe_mxc("codex.exe")
    assert (outcome.verified, outcome.reason) == (False, mxc.REASON_MXC_PROBE_TIMEOUT)
    assert outcome.detail == "codex timed out: slow mount"


def test_explicit_setup_reprobes_a_cached_failure_but_keeps_a_proof(isolated_mxc: Path) -> None:
    calls: list[bool] = []

    def probe(verified: bool) -> Any:
        return lambda binary: calls.append(verified) or mxc.MxcProbe(
            verified,
            mxc.REASON_MXC_VERIFIED if verified else mxc.REASON_MXC_LAUNCH_FAILED,
            "" if verified else "unresolvable mount",
        )

    failed = mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe(False))
    assert failed == {
        "status": "unavailable",
        "reason": mxc.REASON_MXC_LAUNCH_FAILED,
        "detail": "unresolvable mount",
    }
    # A launch reuses the cached failure, with its reason and Codex's words.
    assert mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe(True)) == failed
    # An explicit setup probes again: the mount may be gone.
    retried = mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe(True), retry_unavailable=True)
    assert retried["status"] == "verified" and mxc.mxc_ready("codex.exe", version="0.167.0")
    # A verified proof is reused even by an explicit setup.
    assert (
        mxc.prepare_mxc("codex.exe", "0.167.0", probe=probe(False), retry_unavailable=True)[
            "status"
        ]
        == "verified"
    )
    assert calls == [False, True]


def test_clients_before_the_mount_fixes_never_qualify_mxc() -> None:
    result = mxc.prepare_mxc("codex.exe", "0.162.1", probe=lambda binary: True)
    assert result == {"status": "unavailable", "reason": "codex_mxc_version_unsupported"}


def test_verified_mxc_skips_all_legacy_machine_mutations(isolated_mxc: Path) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("MXC must not create accounts, download legacy helpers, or grant ACLs")

    detection = sandbox_codex.CodexDetection(
        True, "codex.exe", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED, "bundled"
    )
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=detection,
        allow_elevation=False,
        mxc_preparer=lambda *args, **kwargs: {"status": "verified", "reason": "codex_mxc_verified"},
        elevator=forbidden,
        grantor=forbidden,
        helper_preparer=forbidden,
        gate=forbidden,
    )
    assert result.ok and not result.elevated
    assert result.extra["implementation"] == "mxc"


def test_unavailable_mxc_retains_verified_elevated_fallback(isolated_mxc: Path) -> None:
    result = sandbox_cli.provision_codex_windows(
        platform="win32",
        detection=sandbox_codex.CodexDetection(
            True, "codex.exe", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED
        ),
        mxc_preparer=lambda *args, **kwargs: {
            "status": "unavailable",
            "reason": "native_unavailable",
        },
        gate=lambda **kwargs: (True, sandbox_codex.REASON_CODEX_WINDOWS_PROVISIONED),
        grantor=lambda: [],
        allow_elevation=False,
    )
    assert result.ok and result.status == "already_provisioned"


def test_legacy_client_upgrade_requires_new_enforcement_proof() -> None:
    for recorded, expected in (("0.160.0", False), ("0.167.0", True)):
        ready, _ = sandbox_codex.codex_windows_gate(
            platform="win32",
            version="0.167.0",
            provisioned=lambda **kwargs: (True, "accounts_present"),
            marker_reader=lambda recorded=recorded: {
                "codex_version": recorded,
                "enforcement_verified": True,
            },
        )
        assert ready is expected


def test_only_current_mxc_receipt_activates_accountless_gate_and_explicit_policy(
    isolated_mxc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mxc.prepare_mxc("codex.exe", "0.167.0", probe=lambda binary: True)
    assert sandbox_codex.codex_windows_gate(
        platform="win32",
        binary="codex.exe",
        version="0.167.0",
        provisioned=lambda **kwargs: (False, "absent"),
    )[0]
    assert not sandbox_codex.codex_windows_gate(
        platform="win32",
        binary="codex.exe",
        version="0.163.0",
        provisioned=lambda **kwargs: (False, "absent"),
    )[0]
    workspace = isolated_mxc / "workspace"
    command, args = sandbox_codex.compose_codex_spawn(
        [workspace],
        "powershell.exe",
        ["-NoProfile"],
        binary="codex.exe",
        platform="win32",
        codex_home=isolated_mxc,
        version="0.167.0",
    )
    assert command == "codex.exe"
    assert "--include-managed-config" in args
    layer = args[args.index("-p") + 1]
    config = tomllib.loads((isolated_mxc / f"{layer}.config.toml").read_text())
    assert config["windows"]["sandbox"] == "mxc"
    assert config["features"]["network_proxy"] is True
    assert config["permissions"]["clio"]["network"]["domains"] == {"*": "allow"}
    profile = sandbox_codex.synthesize_codex_profile([workspace], platform="win32", mxc=True)
    legacy = sandbox_codex.write_codex_layer(
        "clio", profile, elevated=True, codex_home=isolated_mxc
    )
    assert legacy != layer
    state = sandbox._resolve_backend(
        env={},
        platform="win32",
        codex_detection=sandbox_codex.CodexDetection(
            True, "codex.exe", "0.167.0", sandbox_codex.REASON_CODEX_DETECTED
        ),
    )
    assert state.active
    assert state.details["host_loopback"] == "direct"
    assert sandbox_net.net_mechanism_label(state) == "proxy-enforced-external"


def test_explicit_setup_keeps_uac_workspace_alive_until_helper_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(sandbox_cli, "_launch_elevated_codex_setup"):
        with pytest.raises(RuntimeError, match="win32-only"):
            sandbox_cli._elevated_codex_setup("codex.exe")
        return
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    directories: list[Path] = []

    def launch(argv: list[str]) -> tuple[bool, str]:
        directory = Path(argv[argv.index("-C") + 1])
        assert directory.is_dir()
        directories.append(directory)
        return False, "approval declined"

    monkeypatch.setattr(sandbox_cli, "_launch_elevated_codex_setup", launch)
    assert sandbox_cli._elevated_codex_setup("codex.exe") == (False, "approval declined")
    assert not directories[0].exists()
