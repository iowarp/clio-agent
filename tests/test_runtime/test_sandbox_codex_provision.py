"""B-codex-3 (#1026): Codex Windows provisioning detection + enforcement verify + ladder gate.

Host-agnostic unit coverage — NO real codex/net-user spawn. Every machine-touching seam is
INJECTED: ``runner`` on :func:`sandbox_codex.codex_windows_provisioned` /
:func:`~sandbox_codex.verify_codex_enforcement`, ``codex_provisioned_probe`` on
:func:`sandbox._resolve_backend`, ``provisioned`` / ``marker_reader`` on
:func:`~sandbox_codex.codex_windows_gate`. Pinned #1026 no-false-green invariant: only a
positively provisioned + enforcement-verified state activates codex on win32; every other outcome
floors HONESTLY with a typed reason, never a false ``active``.
"""

from __future__ import annotations

from clio_agent.runtime import sandbox
from clio_agent.runtime import sandbox_codex as sc
from clio_agent.runtime.status import IntegrationState

# --------------------------------------------------------------------------- #
# Injected detection fakes (no real host probe).                               #
# --------------------------------------------------------------------------- #


def _codex_ok() -> sc.CodexDetection:
    return sc.CodexDetection(
        installed=True,
        binary_path="/usr/bin/codex",
        version="0.145.0",
        reason=sc.REASON_CODEX_DETECTED,
    )


def _codex_env() -> dict[str, str]:
    return {"CLIO_SANDBOX_ENABLED": "true"}


# --------------------------------------------------------------------------- #
# codex_windows_provisioned — off-win32 vacuous, account present / absent.      #
# --------------------------------------------------------------------------- #


def test_provisioned_off_windows_is_vacuously_true() -> None:
    """Off-win32 → (True, not_windows): codex uses Seatbelt/bwrap; there is nothing to provision."""
    provisioned, reason = sc.codex_windows_provisioned(platform="linux")
    assert provisioned is True
    assert reason == sc.REASON_NOT_WINDOWS


def test_provisioned_win32_account_present() -> None:
    """win32 + the runner confirms the codexsandboxoffline account → provisioned."""
    provisioned, reason = sc.codex_windows_provisioned(platform="win32", runner=lambda: True)
    assert provisioned is True
    assert reason == sc.REASON_CODEX_WINDOWS_PROVISIONED


def test_provisioned_win32_account_absent() -> None:
    """win32 + the runner says the account is absent → unprovisioned (typed, never a guess)."""
    provisioned, reason = sc.codex_windows_provisioned(platform="win32", runner=lambda: False)
    assert provisioned is False
    assert reason == sc.REASON_CODEX_WINDOWS_UNPROVISIONED


# --------------------------------------------------------------------------- #
# verify_codex_enforcement — off-win32, runner pass-through, fail-safe raise.   #
# --------------------------------------------------------------------------- #


def test_verify_off_windows_is_not_windows() -> None:
    """Off-win32 → (False, not_windows): the Windows enforcement fence does not apply here."""
    enforced, reason = sc.verify_codex_enforcement("codex", "D:\\ws", platform="linux")
    assert enforced is False
    assert reason == sc.REASON_NOT_WINDOWS


def test_verify_runner_verified_passes_through() -> None:
    """A runner that observed a denied out-of-root write → verified."""
    enforced, reason = sc.verify_codex_enforcement(
        "codex.cmd",
        "D:\\ws",
        platform="win32",
        runner=lambda _b, _r: (True, sc.REASON_CODEX_ENFORCEMENT_VERIFIED),
    )
    assert enforced is True
    assert reason == sc.REASON_CODEX_ENFORCEMENT_VERIFIED


def test_verify_runner_escaped_is_reported() -> None:
    """A runner that saw the out-of-root write LAND → escaped (the fence did not hold)."""
    enforced, reason = sc.verify_codex_enforcement(
        "codex.cmd",
        "D:\\ws",
        platform="win32",
        runner=lambda _b, _r: (False, sc.REASON_CODEX_ENFORCEMENT_ESCAPED),
    )
    assert enforced is False
    assert reason == sc.REASON_CODEX_ENFORCEMENT_ESCAPED


def test_verify_runner_unverified_passes_through() -> None:
    """A runner that could not confirm a denial → unverified (the fail-safe verdict)."""
    enforced, reason = sc.verify_codex_enforcement(
        "codex.cmd",
        "D:\\ws",
        platform="win32",
        runner=lambda _b, _r: (False, sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
    )
    assert enforced is False
    assert reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED


def test_verify_runner_raise_is_fail_safe_unverified() -> None:
    """A runner that RAISES (codex could not spawn) → honest unverified, never a false green."""

    def _boom(_b: str, _r: str) -> tuple[bool, str]:
        raise OSError("CreateProcessWithLogonW: access is denied")

    enforced, reason = sc.verify_codex_enforcement(
        "codex.cmd", "D:\\ws", platform="win32", runner=_boom
    )
    assert enforced is False
    assert reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED


# --------------------------------------------------------------------------- #
# codex_windows_gate — the cached ladder default (provisioned AND verified).    #
# --------------------------------------------------------------------------- #


def test_gate_provisioned_and_verified_is_ready() -> None:
    """Account present + marker enforcement_verified True → (True, provisioned)."""
    ready, reason = sc.codex_windows_gate(
        platform="win32",
        provisioned=lambda *, platform: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
        marker_reader=lambda: {"enforcement_verified": True},
    )
    assert ready is True
    assert reason == sc.REASON_CODEX_WINDOWS_PROVISIONED


def test_gate_unprovisioned_floors() -> None:
    """No account → (False, codex_windows_unprovisioned); the marker is never consulted."""
    ready, reason = sc.codex_windows_gate(
        platform="win32",
        provisioned=lambda *, platform: (False, sc.REASON_CODEX_WINDOWS_UNPROVISIONED),
        marker_reader=lambda: (_ for _ in ()).throw(AssertionError("marker must not be read")),
    )
    assert ready is False
    assert reason == sc.REASON_CODEX_WINDOWS_UNPROVISIONED


def test_gate_provisioned_missing_marker_is_unverified() -> None:
    """Account present but NO marker → (False, enforcement_unverified) — no false-green (#1026)."""
    ready, reason = sc.codex_windows_gate(
        platform="win32",
        provisioned=lambda *, platform: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
        marker_reader=lambda: None,
    )
    assert ready is False
    assert reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED


def test_gate_provisioned_marker_not_verified_is_unverified() -> None:
    """Account present + marker enforcement_verified False → (False, enforcement_unverified)."""
    ready, reason = sc.codex_windows_gate(
        platform="win32",
        provisioned=lambda *, platform: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
        marker_reader=lambda: {"enforcement_verified": False},
    )
    assert ready is False
    assert reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED


# --------------------------------------------------------------------------- #
# Ladder gate — win32 codex activates ONLY when provisioned + verified.         #
# --------------------------------------------------------------------------- #


def test_ladder_win32_codex_provisioned_and_verified_activates() -> None:
    """win32 codex + a provisioned+verified probe → MECHANISM_CODEX active."""
    result = sandbox._resolve_backend(
        env=_codex_env(),
        platform="win32",
        codex_detection=_codex_ok(),
        codex_provisioned_probe=lambda: (True, sc.REASON_CODEX_WINDOWS_PROVISIONED),
    )
    assert result.mechanism == sandbox.MECHANISM_CODEX
    assert result.active is True
    assert result.reason == sandbox.REASON_FENCE_ACTIVE
    assert result.details["codex_binary"] == "/usr/bin/codex"


def test_ladder_win32_codex_unprovisioned_floors_typed() -> None:
    """win32 codex + an unprovisioned probe → the honest floor (codex_windows_unprovisioned)."""
    result = sandbox._resolve_backend(
        env=_codex_env(),
        platform="win32",
        codex_detection=_codex_ok(),
        codex_provisioned_probe=lambda: (False, sc.REASON_CODEX_WINDOWS_UNPROVISIONED),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == sc.REASON_CODEX_WINDOWS_UNPROVISIONED


def test_ladder_win32_codex_provisioned_not_verified_floors_no_false_green() -> None:
    """win32 codex + provisioned-but-NOT-verified → floor codex_enforcement_unverified (#1026)."""
    result = sandbox._resolve_backend(
        env=_codex_env(),
        platform="win32",
        codex_detection=_codex_ok(),
        codex_provisioned_probe=lambda: (False, sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED),
    )
    assert result.mechanism == sandbox.MECHANISM_NONE
    assert result.active is False
    assert result.reason == sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED


def test_ladder_non_win32_codex_viable_activates_unchanged() -> None:
    """Off-win32 codex + viable detection → active codex; the provisioning gate is skipped.

    The provisioning probe is a raising sentinel to PROVE it is never consulted off win32.
    """
    result = sandbox._resolve_backend(
        env=_codex_env(),
        platform="linux",
        codex_detection=_codex_ok(),
        codex_provisioned_probe=lambda: (_ for _ in ()).throw(
            AssertionError("win32-only gate must not run off-win32")
        ),
    )
    assert result.mechanism == sandbox.MECHANISM_CODEX
    assert result.active is True
    # Egress is RECORDED via clio's upstream chokepoint (Recipe A) → proxy-enforced net label.
    assert result.details["net_enforcement"] == sandbox.NET_ENFORCEMENT_PROXY


# --------------------------------------------------------------------------- #
# Doctor row — the two new win32 codex floor reasons are DEGRADED + guided.     #
# --------------------------------------------------------------------------- #


def test_doctor_codex_windows_unprovisioned_is_degraded_with_setup_action() -> None:
    """codex_windows_unprovisioned → DEGRADED, next_action points at `clio sandbox setup`."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE,
        active=False,
        reason=sc.REASON_CODEX_WINDOWS_UNPROVISIONED,
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.DEGRADED
    assert "clio sandbox setup" in row.next_action
    assert "Codex Windows fence" in row.next_action


def test_doctor_codex_enforcement_unverified_is_degraded_with_reverify_action() -> None:
    """codex_enforcement_unverified → DEGRADED, next_action says enforcement couldn't be verified."""
    state = sandbox.SandboxResult(
        mechanism=sandbox.MECHANISM_NONE,
        active=False,
        reason=sc.REASON_CODEX_ENFORCEMENT_UNVERIFIED,
    )
    row = sandbox.probe_sandbox(state=state)
    assert row.state == IntegrationState.DEGRADED
    assert "could not be verified" in row.next_action
    assert "clio sandbox setup" in row.next_action
