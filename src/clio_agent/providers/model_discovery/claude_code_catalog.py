"""Fetch the maintained Claude Code model candidates from the CLIO catalog."""

from __future__ import annotations

import re
import threading
from typing import Any

import httpx

CLAUDE_CODE_CATALOG_URL = (
    "https://raw.githubusercontent.com/iowarp/clio-agent/develop/catalogs/claude-code-models.json"
)
_MODEL_ID = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$")
_cache_lock = threading.Lock()
_cached_candidates: list[dict[str, str]] | None = None
_cached_error = ""


class ClaudeCodeCatalogError(RuntimeError):
    """The remote Claude Code catalog could not be read safely."""


def load_claude_code_candidates() -> list[dict[str, str]]:
    """Fetch and validate the current candidates; never substitute bundled data."""

    try:
        response = httpx.get(CLAUDE_CODE_CATALOG_URL, timeout=8.0, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 65_536:
            raise ClaudeCodeCatalogError("Claude Code model catalog is too large")
        payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ClaudeCodeCatalogError(f"Could not fetch Claude Code model catalog: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ClaudeCodeCatalogError("Claude Code model catalog has an unsupported schema")
    rows = payload.get("models")
    if not isinstance(rows, list) or not rows:
        raise ClaudeCodeCatalogError("Claude Code model catalog contains no models")
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ClaudeCodeCatalogError("Claude Code model catalog contains an invalid row")
        model_id = row.get("id")
        name = row.get("name")
        if (
            not isinstance(model_id, str)
            or not _MODEL_ID.fullmatch(model_id)
            or model_id in seen
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 120
        ):
            raise ClaudeCodeCatalogError("Claude Code model catalog contains an invalid model")
        seen.add(model_id)
        candidates.append({"id": model_id, "name": name.strip()})
    return candidates


def refresh_claude_code_candidates() -> list[dict[str, str]]:
    """Replace the process catalog from GitHub, including typed fetch failures."""

    global _cached_candidates, _cached_error
    try:
        candidates = load_claude_code_candidates()
    except ClaudeCodeCatalogError as exc:
        with _cache_lock:
            _cached_candidates = []
            _cached_error = str(exc)
        raise
    with _cache_lock:
        _cached_candidates = candidates
        _cached_error = ""
    return candidates


def cached_claude_code_candidates() -> tuple[list[dict[str, str]] | None, str]:
    """Return the startup/explicit-refresh catalog without doing network I/O."""

    with _cache_lock:
        return (list(_cached_candidates) if _cached_candidates is not None else None), _cached_error


__all__ = [
    "CLAUDE_CODE_CATALOG_URL",
    "ClaudeCodeCatalogError",
    "cached_claude_code_candidates",
    "load_claude_code_candidates",
    "refresh_claude_code_candidates",
]
