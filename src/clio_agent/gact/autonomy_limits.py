"""User-visible limit options shared by the goal and loop commands."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def normalize_limits(values: Mapping[str, Any]) -> dict[str, int | float]:
    """Reject negative, fractional-count and non-finite limits before changing work."""
    result: dict[str, int | float] = {}
    for key, raw in values.items():
        try:
            number = float(raw)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{key} must be a finite nonnegative number") from exc
        integer = key in {"max_iters", "max_goal_iters", "max_tokens"}
        if not math.isfinite(number) or number < 0 or (integer and not number.is_integer()):
            raise ValueError(
                f"{key} must be a finite nonnegative {'integer' if integer else 'number'}"
            )
        result[key] = int(number) if integer else number
    return result


def parse_limit_options(text: str, *, goal: bool = False) -> tuple[str, dict[str, int | float]]:
    """Consume leading --max-iters=N / --max-hours=N / --max-tokens=N options.

    Loops also accept --max-usd=N. The remainder is preserved verbatim as the user's
    instruction; option-looking text after the first prose word is never interpreted.
    """
    names = {
        "--max-iters": "max_goal_iters" if goal else "max_iters",
        "--max-hours": "max_wallclock_s",
        "--max-tokens": "max_tokens",
    }
    if not goal:
        names["--max-usd"] = "max_usd"
    values: dict[str, Any] = {}
    while text.startswith("--"):
        token, _, rest = text.partition(" ")
        option, equal, value = token.partition("=")
        if option not in names or not equal:
            raise ValueError(
                f"Unknown limit option {token!r}; use --max-iters=N, --max-hours=N or --max-tokens=N"
            )
        key = names[option]
        normalized = normalize_limits({key: value})[key]
        values[key] = normalized * 3600 if option == "--max-hours" else normalized
        text = rest.lstrip()
    return text, normalize_limits(values)
