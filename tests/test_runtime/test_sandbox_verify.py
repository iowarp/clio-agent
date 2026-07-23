"""Unit tests for :mod:`clio_agent.runtime.sandbox_verify` (#1026 — Windows fence enforcement).

The real probe spawns srt (win32 only, never unit-run). These tests drive the injectable
``runner`` seam + the off-Windows guard, pinning the fail-safe contract: only a positively
observed denial yields ``True``; every other outcome (raise, escape, spawn failure) is an honest
non-green verdict, so the ladder never claims a fence that is not in force.
"""

from __future__ import annotations

from clio_agent.runtime import sandbox_verify as sv


def test_off_windows_is_not_windows_never_claims_enforcement() -> None:
    """Off-Windows → typed ``not_windows`` (the Windows fence does not apply here)."""
    enforced, reason = sv.verify_windows_enforcement("srt", platform="linux")
    assert enforced is False
    assert reason == sv.REASON_NOT_WINDOWS


def test_runner_verified_passes_through() -> None:
    """A runner that observed a denied out-of-root write → verified."""
    enforced, reason = sv.verify_windows_enforcement(
        "srt.cmd",
        platform="win32",
        runner=lambda _b: (True, sv.REASON_WINDOWS_ENFORCEMENT_VERIFIED),
    )
    assert enforced is True
    assert reason == sv.REASON_WINDOWS_ENFORCEMENT_VERIFIED


def test_runner_escaped_is_reported() -> None:
    """A runner that saw the out-of-root write LAND → escaped (fence did not hold)."""
    enforced, reason = sv.verify_windows_enforcement(
        "srt.cmd",
        platform="win32",
        runner=lambda _b: (False, sv.REASON_WINDOWS_ENFORCEMENT_ESCAPED),
    )
    assert enforced is False
    assert reason == sv.REASON_WINDOWS_ENFORCEMENT_ESCAPED


def test_runner_raise_is_fail_safe_unverified() -> None:
    """A runner that RAISES (srt could not spawn) → honest unverified, never a false green."""

    def _boom(_b: str) -> tuple[bool, str]:
        raise OSError("CreateProcessWithLogonW: access is denied")

    enforced, reason = sv.verify_windows_enforcement("srt.cmd", platform="win32", runner=_boom)
    assert enforced is False
    assert reason == sv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED


def test_runner_unverified_passes_through() -> None:
    """A runner that could not confirm a denial → unverified (the fail-safe verdict)."""
    enforced, reason = sv.verify_windows_enforcement(
        "srt.cmd",
        platform="win32",
        runner=lambda _b: (False, sv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED),
    )
    assert enforced is False
    assert reason == sv.REASON_WINDOWS_ENFORCEMENT_UNVERIFIED
