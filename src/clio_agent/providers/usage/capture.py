"""Passive capture: usage read from what calls already return, at no extra cost."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from clio_agent.providers.usage.api_key_headers import api_key_snapshot
from clio_agent.providers.usage.readers import (
    claude_snapshot,
    codex_snapshot_from_event,
    codex_snapshot_from_headers,
)
from clio_agent.providers.usage.snapshot import USAGE_BOARD

#: The fleet account the Claude Agent SDK and the Codex sign-in serve.
CLAUDE_CODE_ACCOUNT = "claude_code"
CODEX_ACCOUNT = "codex"


def record_rate_limit_usage(event: Any) -> None:
    """Record the windows in a Claude Agent SDK ``RateLimitEvent``."""

    info = getattr(event, "rate_limit_info", None)
    raw = getattr(info, "raw", None)
    if isinstance(raw, Mapping) and (snapshot := claude_snapshot(CLAUDE_CODE_ACCOUNT, raw)):
        USAGE_BOARD.record(snapshot)


def record_codex_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> None:
    """Record the windows in a Codex response's ``x-codex-*`` headers."""

    if snapshot := codex_snapshot_from_headers(CODEX_ACCOUNT, headers):
        USAGE_BOARD.record(snapshot)


def record_codex_event(event: Mapping[str, Any]) -> None:
    """Record the windows in a Codex ``codex.rate_limits`` stream event."""

    if snapshot := codex_snapshot_from_event(CODEX_ACCOUNT, event):
        USAGE_BOARD.record(snapshot)


def record_api_key_headers(
    account: str, headers: Mapping[str, str] | Iterable[tuple[str, str]]
) -> None:
    """Record the windows in an Anthropic or OpenAI API-key response's headers."""

    if snapshot := api_key_snapshot(account, headers):
        USAGE_BOARD.record(snapshot)


__all__ = [
    "record_api_key_headers",
    "record_codex_event",
    "CLAUDE_CODE_ACCOUNT",
    "CODEX_ACCOUNT",
    "record_codex_headers",
    "record_rate_limit_usage",
]
