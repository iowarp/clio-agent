"""Stop before a run when the subscription's 7-day window is nearly used (TD33).

``GET /v1/fleet`` lists accounts; each has ``usage`` (or ``null`` with
``usage_missing_reason``) holding ``windows``: ``{name, used_percent (0 to
100), resets_at, window_minutes}``. For ``claude_code`` the windows are
``5-hour`` (300 minutes) and ``Weekly`` / ``Weekly (Opus)`` / ``Weekly
(Sonnet)`` (10,080 minutes), captured passively from each call's rate-limit
event. The guard reads every 7-day window of the account and stops at the
highest one. Before the first call of a server process there is no reading;
the guard then lets the run start and says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEVEN_DAY_MINUTES = 7 * 24 * 60
FIVE_HOUR_MINUTES = 5 * 60
DEFAULT_ACCOUNT = "claude_code"
DEFAULT_STOP_AT = 0.93


@dataclass(frozen=True)
class GuardDecision:
    """Whether to start the next run, and why."""

    stop: bool
    reason: str
    utilization: float | None = None
    window: str = ""
    resets_at: str = ""


def account_windows(fleet: dict[str, Any], account: str = DEFAULT_ACCOUNT) -> list[dict[str, Any]]:
    """The usage windows of ``account`` in a fleet reading (empty when none)."""
    for row in fleet.get("accounts") or []:
        if row.get("id") == account:
            usage = row.get("usage") or {}
            return [w for w in usage.get("windows") or [] if isinstance(w, dict)]
    return []


def _is_window(window: dict[str, Any], minutes: int, prefix: str) -> bool:
    if window.get("window_minutes") is not None:
        return int(window["window_minutes"]) == minutes
    return str(window.get("name") or "").lower().startswith(prefix)


def seven_day_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [w for w in windows if _is_window(w, SEVEN_DAY_MINUTES, "weekly")]


def five_hour_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [w for w in windows if _is_window(w, FIVE_HOUR_MINUTES, "5-hour")]


def decide(
    fleet: dict[str, Any], *, stop_at: float = DEFAULT_STOP_AT, account: str = DEFAULT_ACCOUNT
) -> GuardDecision:
    """Stop when any 7-day window of ``account`` is at or above ``stop_at`` (a fraction)."""
    weekly = seven_day_windows(account_windows(fleet, account))
    if not weekly:
        reason = next(
            (
                str(row.get("usage_missing_reason") or "no 7-day window in the reading")
                for row in fleet.get("accounts") or []
                if row.get("id") == account
            ),
            f"account {account!r} is not in the fleet",
        )
        return GuardDecision(False, f"no 7-day reading for {account} ({reason}); starting")
    top = max(weekly, key=lambda w: float(w.get("used_percent") or 0.0))
    used = float(top.get("used_percent") or 0.0) / 100.0
    name = str(top.get("name") or "7-day")
    resets = str(top.get("resets_at") or "")
    if used >= stop_at:
        return GuardDecision(
            True,
            f"{account} {name} window at {used:.1%} >= stop-at {stop_at:.1%}"
            + (f"; it resets at {resets}" if resets else ""),
            used,
            name,
            resets,
        )
    return GuardDecision(False, f"{account} {name} window at {used:.1%}", used, name, resets)


def window_deltas(
    before: dict[str, Any] | None, after: dict[str, Any] | None, account: str = DEFAULT_ACCOUNT
) -> dict[str, Any]:
    """Percentage points each window moved between two fleet readings.

    A window whose reset time changed between the readings reset during the
    run; its delta is not comparable and is reported as ``None``.
    """
    old = {w.get("name"): w for w in account_windows(before or {}, account)}
    out: dict[str, Any] = {}
    for window in account_windows(after or {}, account):
        name = str(window.get("name") or "")
        prior = old.get(name)
        entry: dict[str, Any] = {
            "window_minutes": window.get("window_minutes"),
            "after_percent": window.get("used_percent"),
            "before_percent": prior.get("used_percent") if prior else None,
            "delta_points": None,
        }
        if prior and prior.get("resets_at") == window.get("resets_at"):
            entry["delta_points"] = round(
                float(window.get("used_percent") or 0) - float(prior.get("used_percent") or 0), 3
            )
        out[name] = entry
    return out
