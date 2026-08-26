"""Trusted browser-origin resolution for the GACT HTTP service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from clio_agent import conf

_DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]


def gact_cors_origins() -> list[str]:
    """Return configured browser origins or the trusted local defaults."""

    try:
        raw_value: Any = conf.resolve(
            "gact.cors.origins",
            env="CLIO_GACT_CORS_ORIGINS",
            default=None,
        )
    except (TypeError, ValueError):
        return list(_DEFAULT_ORIGINS)
    if raw_value in (None, "", []):
        return list(_DEFAULT_ORIGINS)
    try:
        origins = cast(Callable[[Any], list[str]], conf.as_csv)(raw_value)
    except (TypeError, ValueError):
        return list(_DEFAULT_ORIGINS)
    if origins == ["*"]:
        return origins
    return [origin for origin in origins if origin]
