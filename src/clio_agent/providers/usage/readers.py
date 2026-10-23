"""Turn what each provider reports into a :class:`UsageSnapshot`.

Each reader is a pure function over the provider's own structured fields, so
it is tested with recorded payloads. None of them interprets prose: a field
that is absent is absent, never guessed from a message.

* Claude subscription: the Agent SDK's ``rate_limit_event``. Its raw
  ``rate_limit_info`` carries ``unifiedWindows`` (``five_hour``, ``seven_day``,
  each ``{utilization, resetsAt}``) on every call (checked live 2026-10-23).
* ChatGPT subscription (Codex): the ``x-codex-*`` response headers or the
  ``codex.rate_limits`` stream event (``openai/codex`` ``rate_limits.rs``).
* OpenRouter: ``GET /api/v1/key`` (``data.limit``, ``limit_remaining``, ``usage``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent.providers.usage.snapshot import (
    UsageBalance,
    UsageSnapshot,
    UsageWindow,
    epoch_iso,
    now_iso,
)

#: Display names for the Claude windows the SDK reports.
_CLAUDE_WINDOWS = {
    "five_hour": ("5-hour", 300),
    "seven_day": ("Weekly", 7 * 24 * 60),
    "seven_day_opus": ("Weekly (Opus)", 7 * 24 * 60),
    "seven_day_sonnet": ("Weekly (Sonnet)", 7 * 24 * 60),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def claude_snapshot(account: str, raw: Mapping[str, Any]) -> UsageSnapshot | None:
    """The snapshot in one ``rate_limit_info`` dict, or ``None`` when it names no window."""

    windows: list[UsageWindow] = []
    unified = raw.get("unifiedWindows")
    if isinstance(unified, Mapping):
        for key, value in unified.items():
            if not isinstance(value, Mapping):
                continue
            utilization = _number(value.get("utilization"))
            if utilization is None:
                continue
            name, minutes = _CLAUDE_WINDOWS.get(str(key), (str(key), None))
            windows.append(
                UsageWindow(
                    name=name,
                    used_percent=round(utilization * 100, 1),
                    resets_at=epoch_iso(value.get("resetsAt")),
                    window_minutes=minutes,
                )
            )
    if not windows:
        # An older event names one window at the top level.
        utilization = _number(raw.get("utilization"))
        kind = str(raw.get("rateLimitType") or "")
        if utilization is None or not kind:
            return None
        name, minutes = _CLAUDE_WINDOWS.get(kind, (kind, None))
        windows.append(
            UsageWindow(
                name=name,
                used_percent=round(utilization * 100, 1),
                resets_at=epoch_iso(raw.get("resetsAt")),
                window_minutes=minutes,
            )
        )
    return UsageSnapshot(
        account=account,
        state="metered",
        observed_at=now_iso(),
        source="claude_rate_limit_event",
        windows=tuple(windows),
        status=str(raw.get("status") or ""),
    )


def _codex_window(name: str, used: Any, minutes: Any, reset_at: Any) -> UsageWindow | None:
    percent = _number(used)
    if percent is None:
        return None
    window = _number(minutes)
    return UsageWindow(
        name=name,
        used_percent=round(percent, 1),
        resets_at=epoch_iso(_number(reset_at)),
        window_minutes=int(window) if window else None,
    )


def _codex_name(minutes: Any, fallback: str) -> str:
    window = _number(minutes)
    if window == 300:
        return "5-hour"
    if window == 7 * 24 * 60:
        return "Weekly"
    return fallback


def _codex_balance(has_credits: Any, unlimited: Any, balance: Any) -> UsageBalance | None:
    if str(unlimited).lower() == "true" or unlimited is True:
        return None
    amount = _number(balance)
    if amount is None and str(has_credits).lower() not in {"true", "false"}:
        return None
    return UsageBalance(remaining=amount, currency="credits")


def codex_snapshot_from_headers(
    account: str, headers: Mapping[str, str] | Iterable[tuple[str, str]]
) -> UsageSnapshot | None:
    """The snapshot in Codex's ``x-codex-*`` response headers, or ``None``."""

    pairs = headers.items() if isinstance(headers, Mapping) else headers
    lower = {str(key).lower(): value for key, value in pairs}
    windows = []
    for slot, fallback in (("primary", "Primary"), ("secondary", "Secondary")):
        minutes = lower.get(f"x-codex-{slot}-window-minutes")
        window = _codex_window(
            _codex_name(minutes, fallback),
            lower.get(f"x-codex-{slot}-used-percent"),
            minutes,
            lower.get(f"x-codex-{slot}-reset-at"),
        )
        if window is not None:
            windows.append(window)
    balance = _codex_balance(
        lower.get("x-codex-credits-has-credits"),
        lower.get("x-codex-credits-unlimited"),
        lower.get("x-codex-credits-balance"),
    )
    if not windows and balance is None:
        return None
    return UsageSnapshot(
        account=account,
        state="metered",
        observed_at=now_iso(),
        source="codex_rate_limit_headers",
        windows=tuple(windows),
        balance=balance,
    )


def codex_snapshot_from_event(account: str, event: Mapping[str, Any]) -> UsageSnapshot | None:
    """The snapshot in a ``codex.rate_limits`` stream event, or ``None``."""

    if event.get("type") != "codex.rate_limits":
        return None
    limits = event.get("rate_limits") if isinstance(event.get("rate_limits"), Mapping) else {}
    windows = []
    for slot, fallback in (("primary", "Primary"), ("secondary", "Secondary")):
        row = limits.get(slot)
        if not isinstance(row, Mapping):
            continue
        window = _codex_window(
            _codex_name(row.get("window_minutes"), fallback),
            row.get("used_percent"),
            row.get("window_minutes"),
            row.get("reset_at"),
        )
        if window is not None:
            windows.append(window)
    credits = event.get("credits") if isinstance(event.get("credits"), Mapping) else {}
    balance = _codex_balance(
        credits.get("has_credits"), credits.get("unlimited"), credits.get("balance")
    )
    if not windows and balance is None:
        return None
    return UsageSnapshot(
        account=account,
        state="metered",
        observed_at=now_iso(),
        source="codex_rate_limit_event",
        windows=tuple(windows),
        balance=balance,
        extra={"plan_type": str(event.get("plan_type") or "")} if event.get("plan_type") else {},
    )


def openrouter_snapshot(account: str, body: Mapping[str, Any]) -> UsageSnapshot:
    """The snapshot in an OpenRouter ``GET /api/v1/key`` response.

    Raises:
        ValueError: The body has no ``data`` object (a changed or error response).
    """

    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("openrouter_key_response_without_data")
    limit = _number(data.get("limit"))
    remaining = _number(data.get("limit_remaining"))
    used = _number(data.get("usage"))
    windows = []
    if limit:
        windows.append(
            UsageWindow(
                name=f"Key limit ({data.get('limit_reset') or 'total'})",
                used_percent=round(100 * (limit - (remaining or 0.0)) / limit, 1),
            )
        )
    return UsageSnapshot(
        account=account,
        state="metered",
        observed_at=now_iso(),
        source="openrouter_key_endpoint",
        windows=tuple(windows),
        balance=UsageBalance(remaining=remaining, limit=limit, used=used),
        extra={"is_free_tier": bool(data.get("is_free_tier"))},
    )


__all__ = [
    "claude_snapshot",
    "codex_snapshot_from_event",
    "codex_snapshot_from_headers",
    "openrouter_snapshot",
]
