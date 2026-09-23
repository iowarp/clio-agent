"""Fetch the maintained Claude Code model catalog from the CLIO GitHub document.

Per owner ruling, Claude Code model existence, per-model input-modality
capabilities, and the account default all come from ONE trusted source: this
catalog document (:data:`CLAUDE_CODE_CATALOG_URL`), acquired at startup and on
explicit provider verification -- exactly like Codex's model list is acquired
from the official SDK. CLIO does not probe models through the SDK/CLI to
learn what exists or what a model can do; :mod:`.claude_code` still runs one
small, separate ``auth status`` check to learn whether Claude Code is
installed and signed in, but that check never touches model identity.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from clio_agent.providers.model_discovery.modality_evidence import modality_evidence

CLAUDE_CODE_CATALOG_URL = (
    "https://raw.githubusercontent.com/iowarp/clio-agent/develop/catalogs/claude-code-models.json"
)
_MODEL_ID = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$")
#: The only input modalities the catalog document may declare.
_KNOWN_CAPABILITIES = frozenset({"text", "image", "pdf"})

_cache_lock = threading.Lock()
_cached_catalog: ClaudeCodeCatalog | None = None
_cached_error = ""


class ClaudeCodeCatalogError(RuntimeError):
    """The remote Claude Code catalog could not be read safely."""


@dataclass(frozen=True)
class ClaudeCodeCatalog:
    """One fetched-and-validated snapshot of the maintained Claude Code catalog.

    Attributes:
        models: Rows shaped ``{"id", "name", "capabilities", "capability_evidence"}``.
        default_model: The catalog's declared account default model id, or ``""``
            when the catalog names none.
        default_model_reason: Explains an empty ``default_model`` -- empty itself
            when a default was declared.
    """

    models: list[dict[str, Any]]
    default_model: str
    default_model_reason: str


def _capabilities_from_row(model_id: str, raw: Any) -> tuple[list[str], dict[str, Any]]:
    """Validate one row's ``capabilities`` field; never assume a modality.

    Absent -> ``(["text"], evidence)`` with ``image``/``pdf`` recorded as
    unevidenced -- the row still carries a capability so callers reading
    ``id``/``name``/``capabilities`` never see a missing key, but no non-text
    modality is credited to a model the catalog never described. Present ->
    validated: a non-empty list of unique, known strings that includes
    ``"text"``, else a typed :class:`ClaudeCodeCatalogError` (a caller who
    invents an unknown modality string is a catalog bug, not silently ignored).
    """

    if raw is None:
        return ["text"], modality_evidence(
            source="claude_code_catalog",
            reason="modality_uncataloged",
            unevidenced=("image", "pdf"),
        )
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(value, str) for value in raw)
        or len(set(raw)) != len(raw)
        or not all(value in _KNOWN_CAPABILITIES for value in raw)
        or "text" not in raw
    ):
        raise ClaudeCodeCatalogError(
            f"Claude Code model catalog has invalid capabilities for {model_id!r}"
        )
    return list(raw), modality_evidence(source="claude_code_catalog", reason="modality_cataloged")


def load_claude_code_catalog() -> ClaudeCodeCatalog:
    """Fetch and validate the current catalog snapshot; never substitute bundled data."""

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

    candidates: list[dict[str, Any]] = []
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
        capabilities, evidence = _capabilities_from_row(model_id, row.get("capabilities"))
        candidates.append(
            {
                "id": model_id,
                "name": name.strip(),
                "capabilities": capabilities,
                "capability_evidence": evidence,
            }
        )

    default_model = payload.get("default_model")
    if default_model is None:
        default_model = ""
        default_model_reason = "the maintained Claude Code catalog names no default model"
    elif not isinstance(default_model, str) or default_model not in seen:
        raise ClaudeCodeCatalogError(
            f"Claude Code model catalog default_model {default_model!r} is not a cataloged model id"
        )
    else:
        default_model_reason = ""

    return ClaudeCodeCatalog(
        models=candidates, default_model=default_model, default_model_reason=default_model_reason
    )


def refresh_claude_code_catalog() -> ClaudeCodeCatalog:
    """Replace the process catalog from GitHub, including typed fetch failures."""

    global _cached_catalog, _cached_error
    try:
        catalog = load_claude_code_catalog()
    except ClaudeCodeCatalogError as exc:
        with _cache_lock:
            _cached_catalog = None
            _cached_error = str(exc)
        raise
    with _cache_lock:
        _cached_catalog = catalog
        _cached_error = ""
    return catalog


def cached_claude_code_catalog() -> tuple[ClaudeCodeCatalog | None, str]:
    """Return the startup/explicit-refresh catalog without doing network I/O."""

    with _cache_lock:
        return _cached_catalog, _cached_error


def load_claude_code_candidates() -> list[dict[str, Any]]:
    """Back-compat: fetch the catalog and return its model rows alone.

    Existing callers read each row's ``id``/``name``; rows now also carry
    ``capabilities``/``capability_evidence``.
    """

    return load_claude_code_catalog().models


def refresh_claude_code_candidates() -> list[dict[str, Any]]:
    """Back-compat: refresh the catalog and return its model rows alone."""

    return refresh_claude_code_catalog().models


def cached_claude_code_candidates() -> tuple[list[dict[str, Any]] | None, str]:
    """Back-compat: the cached catalog's model rows alone, or ``(None, error)``."""

    catalog, error = cached_claude_code_catalog()
    return (list(catalog.models) if catalog is not None else None), error


__all__ = [
    "CLAUDE_CODE_CATALOG_URL",
    "ClaudeCodeCatalog",
    "ClaudeCodeCatalogError",
    "cached_claude_code_candidates",
    "cached_claude_code_catalog",
    "load_claude_code_candidates",
    "load_claude_code_catalog",
    "refresh_claude_code_candidates",
    "refresh_claude_code_catalog",
]
