"""The windows in an Anthropic or OpenAI API key's rate-limit response headers.

* Anthropic: ``anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-
  {limit,remaining,reset}``, the reset an RFC 3339 time.
* OpenAI: ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``, the reset a
  duration from now (``1s``, ``6m0s``, ``20ms``).

Like the other readers this is a pure function of the provider's own fields: a
window whose limit or remaining count is missing is left out, never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from clio_agent.providers.usage.snapshot import UsageSnapshot, UsageWindow, now_iso

_ANTHROPIC = (
    ("requests", "Requests"),
    ("tokens", "Tokens"),
    ("input-tokens", "Input tokens"),
    ("output-tokens", "Output tokens"),
)
_OPENAI = (("requests", "Requests"), ("tokens", "Tokens"))
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _count(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _window(
    name: str, limit: str | None, remaining: str | None, resets_at: str
) -> UsageWindow | None:
    cap, left = _count(limit), _count(remaining)
    if cap is None or left is None or cap <= 0:
        return None
    used = min(max((cap - left) / cap * 100, 0.0), 100.0)
    return UsageWindow(name=name, used_percent=round(used, 1), resets_at=resets_at)


def _rfc3339(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return moment.astimezone(UTC).isoformat() if moment.tzinfo else ""


def _after(value: str | None, now: datetime) -> str:
    """``now`` plus an OpenAI reset duration such as ``6m0s``, or ``""``."""

    if not value or _DURATION.sub("", value.strip()):
        return ""
    seconds = sum(float(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION.findall(value))
    return (now + timedelta(seconds=seconds)).isoformat()


def api_key_snapshot(
    account: str, headers: Mapping[str, str] | Iterable[tuple[str, str]]
) -> UsageSnapshot | None:
    """The snapshot in one response's rate-limit headers, or ``None`` without any."""

    pairs = headers.items() if isinstance(headers, Mapping) else headers
    lower = {str(key).lower(): str(value) for key, value in pairs}
    windows: list[UsageWindow] = []
    source = ""
    for slot, name in _ANTHROPIC:
        prefix = f"anthropic-ratelimit-{slot}"
        window = _window(
            name,
            lower.get(f"{prefix}-limit"),
            lower.get(f"{prefix}-remaining"),
            _rfc3339(lower.get(f"{prefix}-reset")),
        )
        if window is not None:
            windows.append(window)
            source = "anthropic_rate_limit_headers"
    now = datetime.now(UTC)
    for slot, name in _OPENAI:
        window = _window(
            name,
            lower.get(f"x-ratelimit-limit-{slot}"),
            lower.get(f"x-ratelimit-remaining-{slot}"),
            _after(lower.get(f"x-ratelimit-reset-{slot}"), now),
        )
        if window is not None:
            windows.append(window)
            source = source or "openai_rate_limit_headers"
    if not windows:
        return None
    return UsageSnapshot(
        account=account,
        state="metered",
        observed_at=now_iso(),
        source=source,
        windows=tuple(windows),
    )


__all__ = ["api_key_snapshot"]
