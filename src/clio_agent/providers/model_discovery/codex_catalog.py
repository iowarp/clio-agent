"""Fetch the maintained Codex model catalog from the CLIO GitHub document.

The Codex backend has no account model-enumeration endpoint the way the old
``openai_codex`` SDK's ``client.models()`` call did, so the model list, each
model's context/output limits, and its reasoning-effort levels come from ONE
trusted source: this catalog document (A.8), acquired at startup and on
explicit provider verification -- exactly mirroring
:mod:`clio_agent.providers.model_discovery.claude_code_catalog`, whose fetch /
cache / offline policy this module reuses unchanged
(:mod:`clio_agent.providers.fetched_catalog`): a disk cache with a TTL and
ETag, atomic writes, and a last-good copy a failed fetch or a failed schema
validation never clears.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from clio_agent.providers.fetched_catalog import FetchedCatalog, FetchedCatalogUnavailable
from clio_agent.providers.thinking import THINKING_LEVELS

CODEX_CATALOG_URL = (
    "https://raw.githubusercontent.com/iowarp/clio-agent/develop/catalogs/codex-models.json"
)
_MODEL_ID = re.compile(r"^[a-z0-9]+(?:[.\-][a-z0-9]+)*$")
_KNOWN_CAPABILITIES = frozenset({"text", "image"})

DEFAULT_TTL_S = 60 * 60.0
_FETCH_TIMEOUT_S = 8.0
_MAX_BYTES = 65_536


class CodexCatalogError(RuntimeError):
    """The maintained Codex catalog could not be read safely."""


@dataclass(frozen=True)
class CodexCatalog:
    """One fetched-and-validated snapshot of the maintained Codex catalog.

    Attributes:
        models: Rows shaped ``{"id", "name", "context_window",
            "max_output_tokens", "reasoning", "effort_levels"}``.
        default_model: The catalog's declared account-agnostic default model
            id, or ``""`` when the catalog names none.
    """

    models: list[dict[str, Any]]
    default_model: str


def _validate_capabilities(model_id: str, raw: Any) -> list[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(value, str) for value in raw)
        or len(set(raw)) != len(raw)
        or not all(value in _KNOWN_CAPABILITIES for value in raw)
        or "text" not in raw
    ):
        raise CodexCatalogError(f"Codex model catalog has invalid capabilities for {model_id!r}")
    return list(raw)


def _validate_effort_levels(model_id: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise CodexCatalogError(f"Codex model catalog has invalid effort_levels for {model_id!r}")
    unknown = [v for v in raw if v not in THINKING_LEVELS]
    if unknown:
        raise CodexCatalogError(
            f"Codex model catalog names unknown effort level(s) {unknown} for {model_id!r}"
        )
    return list(raw)


def _parse_catalog(payload: bytes) -> CodexCatalog:
    """Validate one catalog document's bytes into a :class:`CodexCatalog`.

    Pure parse+validate, no I/O -- the ``parse`` callable handed to
    :class:`~clio_agent.providers.fetched_catalog.FetchedCatalog`.
    """

    try:
        payload_obj: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CodexCatalogError(f"Codex model catalog is not valid JSON: {exc}") from exc

    if not isinstance(payload_obj, dict) or payload_obj.get("schema_version") != 1:
        raise CodexCatalogError("Codex model catalog has an unsupported schema")
    rows = payload_obj.get("models")
    if not isinstance(rows, list) or not rows:
        raise CodexCatalogError("Codex model catalog contains no models")

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CodexCatalogError("Codex model catalog contains an invalid row")
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
            raise CodexCatalogError("Codex model catalog contains an invalid model")
        seen.add(model_id)
        context_window = row.get("context_window")
        max_output_tokens = row.get("max_output_tokens")
        if not isinstance(context_window, int) or context_window <= 0:
            raise CodexCatalogError(
                f"Codex model catalog has an invalid context_window for {model_id!r}"
            )
        if not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
            raise CodexCatalogError(
                f"Codex model catalog has an invalid max_output_tokens for {model_id!r}"
            )
        candidates.append(
            {
                "id": model_id,
                "name": name.strip(),
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                "reasoning": bool(row.get("reasoning", False)),
                "effort_levels": _validate_effort_levels(model_id, row.get("effort_levels")),
                "capabilities": _validate_capabilities(model_id, row.get("capabilities")),
            }
        )

    default_model = payload_obj.get("default_model")
    if default_model is not None and (
        not isinstance(default_model, str) or default_model not in seen
    ):
        raise CodexCatalogError(
            f"Codex model catalog default_model {default_model!r} is not a cataloged model id"
        )
    return CodexCatalog(models=candidates, default_model=str(default_model or ""))


_CATALOG: FetchedCatalog[CodexCatalog] = FetchedCatalog(
    "codex-models",
    CODEX_CATALOG_URL,
    parse=_parse_catalog,
    ttl_s=DEFAULT_TTL_S,
    max_bytes=_MAX_BYTES,
    timeout_s=_FETCH_TIMEOUT_S,
)


def _raise_unavailable(exc: FetchedCatalogUnavailable) -> CodexCatalogError:
    return CodexCatalogError(f"Could not fetch the Codex model catalog: {exc}")


def load_codex_catalog() -> CodexCatalog:
    """Return the current catalog, using the disk cache/network per TTL policy."""

    try:
        return _CATALOG.get().data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def refresh_codex_catalog() -> CodexCatalog:
    """Force a live re-fetch (bypassing TTL freshness), keeping last-good on failure."""

    try:
        return _CATALOG.get(force_refresh=True).data
    except FetchedCatalogUnavailable as exc:
        raise _raise_unavailable(exc) from exc


def cached_codex_catalog() -> tuple[CodexCatalog | None, str]:
    """Return the last known-good catalog without touching the network."""

    try:
        return _CATALOG.get(allow_fetch=False).data, ""
    except FetchedCatalogUnavailable as exc:
        return None, str(exc)


__all__ = [
    "CODEX_CATALOG_URL",
    "CodexCatalog",
    "CodexCatalogError",
    "cached_codex_catalog",
    "load_codex_catalog",
    "refresh_codex_catalog",
]
