"""Owner module for the ``versions`` block on ``GET /v1/capabilities`` (slice A5).

The desktop Versions panel wants, in one place: the agent's own release version,
this backend build's SHA-suffixed identifier, the interpreter it is running on,
the GACT wire-contract version, and the pin/install state of the default
marketplace registry. Building that here (rather than inline in
``routes/system.py``) keeps the route a thin assembly and gives the read a
single, independently testable owner.

Ground rules this module holds itself to (system-cleanup-2026-07.md #775):

* **Cheap and offline.** No network call, no subprocess, no git probe --
  :mod:`clio_agent.gact.blueprint_update_check` (slice A4) already owns the
  ``git ls-remote`` update check; this module only reads already-resolved
  in-process constants and the persisted source ledger.
* **Never raises into capabilities.** A version probe is diagnostic, not
  load-bearing -- any failure degrades to the typed ``"unknown"`` placeholder
  (or ``marketplace=None``) rather than turning ``/v1/capabilities`` into a 500.
* **No silent fallback.** Every degraded field is typed (``"unknown"``), not a
  blank string a client would have to guess the meaning of.
"""

from __future__ import annotations

import logging
import platform
from typing import Any

import clio_agent
from clio_agent.gact.agent_blueprint_sources import load_agent_blueprint_sources
from clio_agent.gact.runtime.constants import CONTRACT_VERSION, GACT_BACKEND_VERSION
from clio_agent.gact.types import MarketplaceVersion, VersionInfo

logger = logging.getLogger(__name__)

_UNKNOWN = "unknown"


def _typed(value: str) -> str:
    """Return ``value``, or the typed ``"unknown"`` placeholder when blank."""

    value = value.strip()
    return value or _UNKNOWN


def _default_source_row() -> dict[str, Any] | None:
    """Return the bundled default marketplace's registered source row, if any.

    Reads the persisted agent-blueprint source ledger
    (:func:`clio_agent.gact.agent_blueprint_sources.load_agent_blueprint_sources`)
    -- a local JSON read, no network/subprocess -- and picks the row flagged
    ``is_default`` by :func:`clio_agent.gact.agent_blueprint_sources.record_default_agent_blueprint_source`.
    Returns ``None`` when the ledger is empty, unreadable, or carries no
    default row (e.g. first boot before bootstrap has run).
    """

    try:
        rows = load_agent_blueprint_sources()
    except Exception:  # noqa: BLE001 - a version probe must never break capabilities
        logger.warning("version_info_source_ledger_read_failed", exc_info=True)
        return None
    return next((row for row in rows if row.get("is_default")), None)


def _marketplace_version() -> MarketplaceVersion | None:
    """Build the marketplace pin block from the default source row, or ``None``."""

    row = _default_source_row()
    if row is None:
        return None
    return MarketplaceVersion(
        source=_typed(str(row.get("source") or "")),
        ref=_typed(str(row.get("ref") or "")),
        pinned_commit=_typed(str(row.get("pinned_commit") or "")),
        installed_commit=_typed(str(row.get("commit") or "")),
        source_id=_typed(str(row.get("id") or "")),
    )


def build_version_info() -> VersionInfo:
    """Build the ``versions`` block for ``GET /v1/capabilities``.

    Every field is resolved from already-computed, in-process values
    (:data:`clio_agent.__version__`, :data:`GACT_BACKEND_VERSION`,
    :func:`platform.python_version`, :data:`CONTRACT_VERSION`) plus one local
    JSON read for the marketplace pin -- no network access, no subprocess, so
    this stays cheap enough to run on every ``/v1/capabilities`` request.

    Returns:
        A :class:`~clio_agent.gact.types.VersionInfo`. Never raises: an
        unresolvable scalar field types as ``"unknown"``, and a missing or
        unreadable marketplace source row types as ``marketplace=None``.
    """

    try:
        marketplace = _marketplace_version()
    except Exception:  # noqa: BLE001 - marketplace pin is best-effort, never fatal
        logger.warning("version_info_marketplace_build_failed", exc_info=True)
        marketplace = None
    return VersionInfo(
        clio_agent=_typed(str(getattr(clio_agent, "__version__", "") or "")),
        backend_build=_typed(str(GACT_BACKEND_VERSION or "")),
        python=_typed(platform.python_version()),
        gact_contract=_typed(str(CONTRACT_VERSION or "")),
        marketplace=marketplace,
    )
