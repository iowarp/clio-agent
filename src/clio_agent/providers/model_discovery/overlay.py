"""The refresh overlay: read, dual-key lookup, atomic merge-write with delta
reporting (iowarp/clio-agent#1211).

Persisted to the user data dir (a JSON file sibling to the handshake's
``model_limits.json`` — see :mod:`clio_agent.providers.handshake.sources.db`),
read overlay-first / static-fallback by ``GET /v1/providers/{id}/models`` for
the CLI provider kinds ONLY (HTTP-backed providers always keep their live
handshake path — #1211 review D5) and consulted — never live-reprobed — by the
passive handshake seam (:mod:`clio_agent.providers.handshake.cli_catalog`).

No-silent-fallback (CLAUDE.md cleanup-program ground rule): a probe failure for
one provider NEVER clears that provider's existing overlay entry — the previous
good list plus a typed ``failed_reason`` are both recorded, and a malformed
on-disk overlay raises :class:`OverlayMalformedError` rather than silently
degrading to ``{}`` (the #1202 ``_read_mcp_yaml`` lesson). :func:`record_refresh`
also refuses to write a claimed-success-but-empty result (#1211 review R1) — a
bug upstream must not silently narrow the overlay to nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.providers.catalog import as_cloud_api_key_env

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

CODEX_SOURCE = "codex_app_server"
CLAUDE_CODE_SOURCE = "claude_code_alias_probe"
HTTP_SOURCE = "live_handshake"

#: Owner ruling 2026-08-14: claude_code's SERVED/BOUND default is a deliberate
#: cost policy, not the CLI's own choice. A bare ``claude -p`` (no ``--model``)
#: resolves to ``claude-fable-5`` -- the most expensive tier -- and clio must
#: never silently default a user onto it. "sonnet" (documented alias, second
#: from the top) is the policy default instead. This affects ONLY the
#: unrequested/omitted-model case for claude_code; fable stays fully available
#: for explicit selection, and codex is unaffected (keeps following its own
#: account default). See :func:`record_refresh`, which is the single seam that
#: applies this -- the overlay's ``default_model`` is always "what clio
#: serves", and the CLI's own (still-recorded, honest) choice lives alongside
#: it under ``cli_default``.
CLAUDE_CODE_COST_DEFAULT_MODEL = "sonnet"


class OverlayMalformedError(RuntimeError):
    """The on-disk model-catalog overlay exists but is not a valid JSON object.

    Raised instead of silently degrading to ``{}`` (the #1202 ``_read_mcp_yaml``
    lesson). The diagnostic ``GET``/``POST`` routes let this surface as a typed
    HTTP error; the passive/ambient handshake read path
    (:mod:`clio_agent.providers.handshake.cli_catalog`) catches it and falls back
    to the static registry catalog, documenting that decision explicitly.
    """


class OverlayUnreadableError(OverlayMalformedError):
    """The on-disk overlay file exists but could not even be READ (I/O error).

    Distinct from a malformed-CONTENT overlay (#1211 review N1): a permission
    error, a locked file, or a transient disk fault is an ENVIRONMENTAL failure,
    not evidence the JSON itself is corrupt. Subclasses :class:`OverlayMalformedError`
    so every existing ``except OverlayMalformedError`` catch site keeps working
    unchanged; callers that need to tell the two apart can ``isinstance`` check.
    """


@dataclass
class ProviderDiscoveryResult:
    """One provider's discovery outcome — the ``POST .../refresh`` per-row shape.

    ``discovered`` and ``default_model`` are only meaningful when
    ``failed_reason`` is unset; a failed probe carries an empty ``discovered``
    list and :func:`record_refresh` keeps whatever the overlay already held.
    """

    provider: str
    discovered: list[dict[str, Any]]
    source: str
    default_model: str = ""
    #: Set when the default_model above is a FALLBACK (first validated
    #: candidate) rather than a CLI-verified match — e.g. the bare
    #: no-``--model`` probe that identifies the CLI's own live default was
    #: itself inconclusive/rejected (#1211 review N5). Empty when
    #: ``default_model`` is CLI-verified.
    default_model_reason: str = ""
    failed_reason: str | None = None
    #: Individually-rejected candidates on an otherwise-successful probe (e.g. one
    #: claude_code alias 404s while the others validate) — informational, never
    #: silently dropped.
    rejected: list[dict[str, str]] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def overlay_path() -> Path:
    """The writable overlay file: ``CLIO_MODEL_CATALOG`` env override, else the
    user data dir — a sibling of the handshake's ``model_limits.json``
    (:func:`clio_agent.providers.handshake.sources.db.db_path`)."""
    from clio_agent import (
        conf,  # noqa: PLC0415 - avoid import cycle at module load
        paths,  # noqa: PLC0415 - avoid import cycle at module load
    )

    override = conf.resolve(
        "paths.model_catalog", env="CLIO_MODEL_CATALOG", default="", cast=conf.as_str
    ).strip()
    if override:
        return Path(override).expanduser()
    return paths.user_data_dir() / "model_catalog.json"


def read_overlay() -> dict[str, dict[str, Any]]:
    """Return the raw overlay dict (``{}`` when the file is absent).

    Raises :class:`OverlayUnreadableError` when the file exists but a plain read
    fails (I/O error — #1211 N1), or :class:`OverlayMalformedError` when it reads
    fine but is not valid JSON / not a JSON object. Never silently swallowed to
    ``{}`` in either case.
    """
    path = overlay_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OverlayUnreadableError(f"model catalog overlay unreadable at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OverlayMalformedError(
            f"model catalog overlay at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise OverlayMalformedError(
            f"model catalog overlay at {path} must be a JSON object keyed by provider id"
        )
    return data


def overlay_models_wire(provider_id: str, provider_kind: str) -> dict[str, Any] | None:
    """Return the overlay-first ``{"models", "source", ...}`` wire dict, or ``None``.

    Looks up by preset id first, then bare provider_kind (mirrors
    :func:`clio_agent.providers.catalog.as_provider_models_dict`'s dual-keying).
    ``None`` means "no usable overlay entry" (absent, or present with an empty
    ``models`` list because this provider has never successfully discovered) —
    callers fall back to the static catalog. Propagates
    :class:`OverlayMalformedError` for a corrupt on-disk file (never silently {}).
    """
    db = read_overlay()
    entry = db.get(provider_id) or db.get(provider_kind)
    if not isinstance(entry, dict):
        return None
    models = entry.get("models")
    if not isinstance(models, list) or not models:
        return None
    wire: dict[str, Any] = {
        "models": models,
        "source": str(entry.get("source") or "overlay"),
        "default_model": str(entry.get("default_model") or ""),
        "generated_at": str(entry.get("generated_at") or ""),
    }
    if entry.get("rejected"):
        wire["rejected"] = entry["rejected"]
    if entry.get("cli_default"):
        wire["cli_default"] = entry["cli_default"]
    return wire


def overlay_default_model(provider_id: str, provider_kind: str) -> str:
    """Return the overlay's discovered ``default_model`` for a provider, or ``""``.

    Consulted by surfaces that pick a model with no explicit request — the
    provider-list ``default_model`` row and an omitted-model bind (#1211 review
    D2) — so they follow the CLI's OWN live default (once a refresh has run)
    instead of the frozen static ``suggested_model``. A malformed overlay
    degrades to ``""`` here (never crashes a listing/bind path); the failure is
    logged, never silent.
    """
    try:
        db = read_overlay()
    except OverlayMalformedError as exc:
        logger.warning("model catalog overlay malformed reading default_model: %s", exc)
        return ""
    entry = db.get(provider_id) or db.get(provider_kind)
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("default_model") or "")


def resolve_cloud_api_key(provider_kind: str) -> str:
    """Resolve a cloud provider's API key: its dedicated env var, else ``CLIO_LM_API_KEY``."""
    env_name = as_cloud_api_key_env().get(provider_kind, "")
    key = os.environ.get(env_name, "") if env_name else ""
    return key or os.environ.get("CLIO_LM_API_KEY", "")


def attach_context_limits(
    discovered: list[dict[str, Any]], provider_kind: str
) -> list[dict[str, Any]]:
    """Resolve + attach each discovered model's context/output limits (#1211 D4).

    Calls the SAME cascade the handshake's ``enrich_capabilities`` step would
    (models.dev -> litellm catalog -> local DB) but does it ONCE, here, at
    explicit refresh time, and persists the result (a hit OR a definitive miss)
    onto the overlay row. :class:`~clio_agent.providers.handshake.cli_catalog.
    CliCatalogHandshake` then reads it back pre-filled and skips the cascade
    entirely on every later passive/ambient handshake call (connect, doctor, a
    bind) — closing the REAL cost D4 identifies: the cascade attempting a
    models.dev fetch (network-timeout-bounded, not instant) on every one of
    those calls when its on-disk cache happens to be stale, regardless of which
    model id is being resolved.
    """
    from clio_agent.providers.handshake.sources import (  # noqa: PLC0415
        resolve_context,
        resolve_output_limit,
    )

    enriched: list[dict[str, Any]] = []
    for m in discovered:
        row: dict[str, Any] = dict(m)
        model_id = str(row.get("id") or "")
        window, source = resolve_context(model_id, provider_kind)
        row["context_window"] = window
        row["context_source"] = source
        row["output_limit"] = resolve_output_limit(model_id, provider_kind)
        enriched.append(row)
    return enriched


def record_refresh(result: ProviderDiscoveryResult) -> dict[str, Any]:
    """Merge one provider's discovery result into the overlay; return its wire row.

    A probe failure (``result.failed_reason`` set, ``discovered`` empty) NEVER
    clears an existing ``models`` entry — the prior good list is kept verbatim
    and the failure is recorded alongside it (no-silent-fallback). A successful
    discovery overwrites ``models``/``source``/``default_model``/``generated_at``
    and clears any stale ``failed_reason``. ``added``/``removed``/``unchanged``
    are the model-id delta against whatever the entry held before this call —
    empty added/removed on a failed probe, since the served list didn't change.

    Refuses (raises :class:`ValueError`) a result that claims success (no
    ``failed_reason``) but carries an empty ``discovered`` list — a write-boundary
    guard (#1211 review R1): a caller bug upstream must not silently narrow the
    overlay to nothing. Propagates :class:`OverlayMalformedError` — a refresh
    cannot safely read-modify-write onto a corrupt file; it must surface, never
    silently discard whatever other providers' good data the file held.
    """
    if not result.failed_reason and not result.discovered:
        raise ValueError(
            f"record_refresh: provider={result.provider!r} claims success (no "
            "failed_reason) but discovered=[] -- refusing to write an empty "
            "models list (write-boundary guard, #1211 R1)"
        )
    path = overlay_path()
    with _LOCK:
        db = read_overlay()
        previous = db.get(result.provider)
        previous_models = previous.get("models") if isinstance(previous, dict) else None
        previous_ids = {
            str(m["id"]) for m in (previous_models or []) if isinstance(m, dict) and m.get("id")
        }
        entry: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
        if result.failed_reason:
            entry["failed_reason"] = result.failed_reason
            entry["last_attempt_at"] = result.generated_at
            new_ids = previous_ids  # nothing served changes on a failed probe
        else:
            entry["models"] = result.discovered
            entry["source"] = result.source
            entry["default_model"] = result.default_model
            entry["generated_at"] = result.generated_at
            entry.pop("failed_reason", None)
            if result.default_model_reason:
                entry["default_model_reason"] = result.default_model_reason
            else:
                entry.pop("default_model_reason", None)
            if result.rejected:
                entry["rejected"] = result.rejected  # #1211 N3: persisted, not just in the wire row
            else:
                entry.pop("rejected", None)
            # Owner ruling 2026-08-14 (cost-aware default): claude_code's SERVED
            # default_model is the policy value, not the CLI's raw choice -- the
            # CLI's own honest default rides along under cli_default (never
            # dropped) for the /update-models delta report + observability. Only
            # overrides when the policy model actually validated for this account
            # (never points the default at something the account doesn't serve);
            # codex and every other provider kind are untouched.
            if result.provider == "claude_code":
                entry["cli_default"] = result.default_model
                if any(
                    isinstance(m, dict) and m.get("id") == CLAUDE_CODE_COST_DEFAULT_MODEL
                    for m in result.discovered
                ):
                    entry["default_model"] = CLAUDE_CODE_COST_DEFAULT_MODEL
            else:
                entry.pop("cli_default", None)
            new_ids = {str(m["id"]) for m in result.discovered if m.get("id")}
        db[result.provider] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        # #1211 review R7: a uuid-suffixed temp name so two concurrent refreshes
        # (or a refresh racing a stray writer) never collide on the same tmp file.
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(db, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    wire: dict[str, Any] = {
        "provider": result.provider,
        "discovered": entry.get("models", []),
        "source": entry.get("source", result.source),
        "default_model": entry.get("default_model", ""),
        "generated_at": result.generated_at,
        "added": sorted(new_ids - previous_ids),
        "removed": sorted(previous_ids - new_ids),
        "unchanged": sorted(new_ids & previous_ids),
    }
    if result.failed_reason:
        wire["failed_reason"] = result.failed_reason
    if entry.get("default_model_reason"):
        wire["default_model_reason"] = entry["default_model_reason"]
    if entry.get("rejected"):
        wire["rejected"] = entry["rejected"]
    # The CLI's own honest default (claude_code only -- #1211 cost-policy
    # ruling 2026-08-14), distinct from the served ``default_model`` above so
    # the /update-models delta can report both explicitly.
    if entry.get("cli_default"):
        wire["cli_default"] = entry["cli_default"]
    return wire


__all__ = [
    "CLAUDE_CODE_COST_DEFAULT_MODEL",
    "CLAUDE_CODE_SOURCE",
    "CODEX_SOURCE",
    "HTTP_SOURCE",
    "OverlayMalformedError",
    "OverlayUnreadableError",
    "ProviderDiscoveryResult",
    "attach_context_limits",
    "overlay_default_model",
    "overlay_models_wire",
    "overlay_path",
    "read_overlay",
    "record_refresh",
    "resolve_cloud_api_key",
]
