"""Fetch the maintained Claude Code model catalog from the CLIO GitHub document.

Per owner ruling, Claude Code model existence, per-model input-modality
capabilities, and the account default all come from ONE trusted source: this
catalog document (:data:`CLAUDE_CODE_CATALOG_URL`), acquired at startup and on
explicit provider verification -- exactly like Codex's model list is acquired
from the official SDK. CLIO does not probe models through the SDK/CLI to
learn what exists or what a model can do; :mod:`.claude_code` still runs one
small, separate ``auth status`` check to learn whether Claude Code is
installed and signed in, but that check never touches model identity.

Fetch / cache / offline policy is owned by the generic
:mod:`clio_agent.providers.fetched_catalog` mechanism: a disk cache with a TTL
and ETag under ``paths.user_cache_dir()/catalogs/claude-code-models.json``,
atomic writes, and a last-good copy that a failed fetch or a failed schema
validation never clears (this used to be an in-memory-only cache that a failed
fetch CLEARED outright -- a transient GitHub hiccup used to take the whole
catalog down with it; it no longer does). There is no bundled cold-start
fallback: a true cold start with no disk cache and no network is a typed
:class:`ClaudeCodeCatalogError`, same as before.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from clio_agent.providers.fetched_catalog import (
    FetchedCatalog,
    FetchedCatalogUnavailable,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.thinking_levels import THINKING_LEVELS

CLAUDE_CODE_CATALOG_URL = (
    "https://raw.githubusercontent.com/iowarp/clio-agent/develop/catalogs/claude-code-models.json"
)
_MODEL_ID = re.compile(r"^claude-[a-z0-9]+(?:-[a-z0-9]+)*$")
#: The only input modalities the catalog document may declare.
_KNOWN_CAPABILITIES = frozenset({"text", "image", "pdf"})

#: How long a fetched catalog is served before a normal (non-forced) read
#: re-fetches. Short: this is a small, cheap document and staying current
#: matters more than saving a request.
DEFAULT_TTL_S = 60 * 60.0

_FETCH_TIMEOUT_S = 8.0
_MAX_BYTES = 65_536


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


def _parse_catalog(payload: bytes) -> ClaudeCodeCatalog:
    """Validate one catalog document's bytes into a :class:`ClaudeCodeCatalog`.

    Pure parse+validate, no I/O -- the ``parse`` callable handed to
    :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`. Raises
    :class:`ClaudeCodeCatalogError` on any schema violation, which
    ``FetchedCatalog`` treats as a validation failure (never substituted
    silently; the last good copy rides through instead).
    """

    try:
        payload_obj: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ClaudeCodeCatalogError(f"Claude Code model catalog is not valid JSON: {exc}") from exc

    if not isinstance(payload_obj, dict) or payload_obj.get("schema_version") != 1:
        raise ClaudeCodeCatalogError("Claude Code model catalog has an unsupported schema")
    rows = payload_obj.get("models")
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
        candidate: dict[str, Any] = {
            "id": model_id,
            "name": name.strip(),
            "capabilities": capabilities,
            "capability_evidence": evidence,
        }
        shipped_default = row.get("shipped_default_effort")
        if shipped_default is not None:
            if not isinstance(shipped_default, str) or shipped_default not in THINKING_LEVELS:
                raise ClaudeCodeCatalogError(
                    f"Claude Code model catalog has an invalid shipped_default_effort "
                    f"for {model_id!r}: {shipped_default!r}"
                )
            candidate["shipped_default_effort"] = shipped_default
        candidates.append(candidate)

    default_model = payload_obj.get("default_model")
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


_CATALOG: FetchedCatalog[ClaudeCodeCatalog] = FetchedCatalog(
    "claude-code-models",
    CLAUDE_CODE_CATALOG_URL,
    parse=_parse_catalog,
    ttl_s=DEFAULT_TTL_S,
    max_bytes=_MAX_BYTES,
    timeout_s=_FETCH_TIMEOUT_S,
)


def _raise_unavailable(exc: FetchedCatalogUnavailable) -> ClaudeCodeCatalogError:
    return ClaudeCodeCatalogError(f"Could not fetch Claude Code model catalog: {exc}")


def load_claude_code_catalog() -> ClaudeCodeCatalog:
    """Return the current catalog, using the disk cache/network per TTL policy.

    Unlike before the fetched_catalog migration, a transient fetch failure no
    longer fails this call outright when a previously-good copy is cached on
    disk -- it is served instead, with the degradation logged as a typed
    ``stale_reason`` (see :mod:`clio_agent.providers.fetched_catalog`). This
    only raises :class:`ClaudeCodeCatalogError` on a true total miss: no cache,
    no network (there is no bundled fallback for this catalog).
    """

    try:
        return _CATALOG.get().data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def refresh_claude_code_catalog() -> ClaudeCodeCatalog:
    """Force a live re-fetch (bypassing TTL freshness), keeping last-good on failure.

    Still typed-fails (:class:`ClaudeCodeCatalogError`) only when there is
    truly nothing usable: no disk cache at all AND the live fetch failed.
    """

    try:
        return _CATALOG.get(force_refresh=True).data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def cached_claude_code_catalog() -> tuple[ClaudeCodeCatalog | None, str]:
    """Return the last known-good catalog without touching the network.

    Disk-only (no HTTP), matching the previous in-memory-only accessor's
    contract of "no network I/O" -- the disk cache now makes this durable
    across process restarts, which the in-memory version never was.
    """

    try:
        return _CATALOG.get(allow_fetch=False).data, ""
    except FetchedCatalogUnavailable as exc:
        return None, str(exc)


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
