"""Usage snapshots: what an account has used and has left.

One shape for every account kind (design: ``docs/design/sdk/model-fleet.md``,
"Design: usage"): a list of windows (a subscription's 5-hour and weekly
limits, say), an optional balance (OpenRouter credit), when it was observed
and from what. An account with no quota at all reports ``unmetered`` rather
than nothing, so a reader never mistakes "no limits" for "no data".

Snapshots are captured passively from what calls already return (the Claude
Agent SDK's rate-limit event, Codex's rate-limit headers) or read on demand
from a provider's usage endpoint. The board below holds the latest one per
account for this process; a failed read keeps the last good snapshot and
records why beside it, so a value is never cleared by a failure.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

UsageState = Literal["metered", "unmetered"]


@dataclass(frozen=True)
class UsageWindow:
    """One usage window: how much of it is used and when it resets."""

    name: str
    used_percent: float
    resets_at: str = ""
    window_minutes: int | None = None


@dataclass(frozen=True)
class UsageBalance:
    """Money left on a credit account."""

    remaining: float | None
    limit: float | None = None
    used: float | None = None
    currency: str = "USD"


@dataclass(frozen=True)
class UsageSnapshot:
    """The latest known usage of one account."""

    account: str
    state: UsageState
    observed_at: str
    source: str
    windows: tuple[UsageWindow, ...] = ()
    balance: UsageBalance | None = None
    #: The account's own status word when it gives one (``allowed_warning``, ``rejected``).
    status: str = ""
    #: Why the latest read failed, when it did; the values above are then the last good ones.
    read_failure: str = ""
    read_failed_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def epoch_iso(value: Any) -> str:
    """A Unix timestamp (seconds) as ISO-8601, or ``""`` when it is not one."""

    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return ""
    return datetime.fromtimestamp(float(value), UTC).isoformat()


def unmetered(account: str, *, source: str = "local_endpoint") -> UsageSnapshot:
    """The snapshot of an account that has no quota to report."""

    return UsageSnapshot(account=account, state="unmetered", observed_at=now_iso(), source=source)


class UsageBoard:
    """The latest snapshot per account, for this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, UsageSnapshot] = {}

    def record(self, snapshot: UsageSnapshot) -> None:
        with self._lock:
            self._latest[snapshot.account] = snapshot

    def record_failure(self, account: str, reason: str) -> None:
        """Note a failed read, keeping the last good snapshot when there is one."""

        with self._lock:
            previous = self._latest.get(account) or UsageSnapshot(
                account=account, state="metered", observed_at="", source=""
            )
            self._latest[account] = replace(previous, read_failure=reason, read_failed_at=now_iso())

    def latest(self, account: str) -> UsageSnapshot | None:
        with self._lock:
            return self._latest.get(account)

    def all(self) -> dict[str, UsageSnapshot]:
        with self._lock:
            return dict(self._latest)

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()


#: The process-wide board every capture point writes to.
USAGE_BOARD = UsageBoard()


__all__ = [
    "USAGE_BOARD",
    "UsageBalance",
    "UsageBoard",
    "UsageSnapshot",
    "UsageState",
    "UsageWindow",
    "epoch_iso",
    "now_iso",
    "unmetered",
]
