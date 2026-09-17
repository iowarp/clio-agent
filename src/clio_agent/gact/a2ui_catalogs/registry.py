"""The server-side A2UI catalog registry — the protocol's unit of trust.

Replaces the single hard-coded ``CLIO_A2UI_CATALOG_ID`` equality check
(deleted, docs/design/a2ui-compat-campaign-2026-09.md S2) with a real
registry keyed ``(catalogId, protocolVersion)``: the two builtin catalogs
(Basic, CLIO workspace) plus every catalog a discovered Agent Blueprint pack
declares. ``CatalogRegistry`` never decides what a session may PRODUCE — that
is ``activation.session_producible_catalog_ids`` — it only answers "does this
id resolve to a catalog this server can validate against."

**Cache doctrine** (adversarial review, BLOCKING perf: a POST that reached
message #30 in a session cost ~2s, entirely spent re-discovering Agent
Blueprints from disk on every single message). A cache accelerates; it never
owns truth, and staleness is explicit, never an implicit per-call
re-validation:

- Builtin catalogs are immutable package data, loaded once at construction;
  ``.get()`` checks them FIRST with zero I/O, regardless of pack state.
- Blueprint discovery (``discover_agent_blueprints()`` — a real filesystem
  scan + per-``AGENT.md`` parse) and the pack-catalog list it produces are
  each cached on the instance after the first call. The cache is exactly as
  fresh as the last install/update/uninstall — it is invalidated ONLY by an
  explicit :meth:`CatalogRegistry.invalidate` call from the blueprint
  mutation routes (``routes/blueprints.py``), never by re-scanning per
  lookup. A session that never installs/uninstalls a pack pays the discovery
  cost once, not once per message.
- Validator compilation (``jsonschema.Draft202012Validator``, via
  ``clio_schemas.a2ui.validation.catalog_validators``) is additionally cached
  by the catalog file's content checksum, so a pack catalog whose bytes are
  unchanged across a cache rebuild reuses its compiled validators.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from clio_schemas.a2ui.sidecar import CatalogSidecar
from clio_schemas.a2ui.validation import catalog_validators
from jsonschema import Draft202012Validator

from clio_agent.gact.protocol.constants import A2UI_V091

CatalogSource = Literal["builtin", "blueprint"]


@dataclass(frozen=True)
class CatalogEntry:
    """One resolved, validator-compiled A2UI catalog.

    Attributes:
        catalog_id: The catalog file's own ``catalogId`` (a stable URI-style
            identifier; the wire's negotiated key).
        protocol_version: The A2UI protocol version this catalog targets
            (``"0.9.1"`` for every catalog in this slice).
        file: The raw official catalog-file document (``$schema``, ``$id``,
            ``catalogId``, ``components``, ``functions``, ``$defs``).
        sidecar: CLIO's packaging metadata for this catalog (kernel
            implementations, event destinations, trust source).
        instructions: The catalog's producer-guidance Markdown.
        source: Where this catalog came from.
        root_path: The directory the catalog's three files were read from.
        checksum: Content checksum of ``file`` (validator cache key).
        validators: One compiled ``Draft202012Validator`` per component name.
        install_checksum: The OWNING pack's install checksum
            (``.clio-install.md``, stamped like ``blueprint_server_map``'s
            ``CLIO_BLUEPRINT_INSTALL_CHECKSUM``) -- distinct from
            ``checksum`` (the catalog FILE's own bytes). Empty for a builtin
            catalog, which has no install lifecycle.
        name: The short local slug this catalog is known by beside its full
            ``catalogId`` -- ``"basic"``/``"clio-workspace"`` for the two
            builtins, or the pack's own ``a2ui_catalogs:`` key for a
            blueprint-declared catalog (``blueprint.py``'s
            ``blueprint_catalog_map`` keys, NOT derived from ``root_path``,
            which may differ from the declared name via a custom ``reldir``).
            The sole consumer today is :mod:`clio_agent.gact.a2ui_catalogs.
            skills` (S4), which mints the ``a2ui-catalog-<name>`` skill id
            from it -- kept on the entry rather than re-derived by callers so
            there is exactly one place that decides a catalog's short name.
        catalog_file_path: The ``catalog.json`` FILE's own path -- distinct
            from ``root_path`` because the vendored Basic catalog's layout is
            asymmetric (``builtin.py``'s module docstring): its
            ``catalog.json`` lives beside the rest of the vendored 0.9.1
            spec, while ``root_path`` (its sidecar/instructions directory)
            does not contain it. Defaults to ``root_path / "catalog.json"``,
            true for every catalog except vendored Basic. The catalog-skill
            bundled-file root (S4, :mod:`clio_agent.gact.a2ui_catalogs.
            skills`) is this path's PARENT, so ``load_skill(id,
            file="catalog.json#/...")`` reads the same official bytes the
            server validates against for every catalog, Basic included.
    """

    catalog_id: str
    protocol_version: str
    file: dict[str, Any]
    sidecar: CatalogSidecar
    instructions: str
    source: CatalogSource
    root_path: Path
    checksum: str
    validators: dict[str, Draft202012Validator]
    install_checksum: str = ""
    name: str = ""
    catalog_file_path: Path = Path()


class CatalogResolver(Protocol):
    """The minimal shape ``gact/a2ui.py`` validation needs from a registry."""

    def get(self, catalog_id: str, protocol_version: str = A2UI_V091) -> "CatalogEntry | None":
        """Return the catalog entry for ``(catalog_id, protocol_version)``, or ``None``."""
        ...


_VALIDATOR_CACHE: dict[str, dict[str, Draft202012Validator]] = {}
_VALIDATOR_CACHE_LOCK = threading.Lock()


def compiled_validators(checksum: str, file: Mapping[str, Any]) -> dict[str, Draft202012Validator]:
    """Return this catalog file's per-component validators, cached by checksum.

    Args:
        checksum: Content checksum of ``file`` (see
            :func:`clio_agent.gact.a2ui_catalogs.blueprint.catalog_checksum`).
        file: The raw catalog-file document.

    Returns:
        ``{component_name: compiled validator}``, built once per distinct
        checksum and reused thereafter.
    """

    with _VALIDATOR_CACHE_LOCK:
        cached = _VALIDATOR_CACHE.get(checksum)
    if cached is not None:
        return cached
    compiled = catalog_validators(file)
    with _VALIDATOR_CACHE_LOCK:
        return _VALIDATOR_CACHE.setdefault(checksum, compiled)


def make_entry(
    *,
    file: dict[str, Any],
    sidecar: CatalogSidecar,
    instructions: str,
    source: CatalogSource,
    root_path: Path,
    checksum: str,
    install_checksum: str = "",
    name: str = "",
    catalog_file_path: "Path | None" = None,
) -> CatalogEntry:
    """Build one :class:`CatalogEntry`, compiling (or reusing cached) validators.

    ``catalog_file_path`` defaults to ``root_path / "catalog.json"`` -- true
    for every catalog except the vendored Basic catalog, whose loader passes
    the real path explicitly (see :attr:`CatalogEntry.catalog_file_path`).
    """

    return CatalogEntry(
        catalog_id=str(file["catalogId"]),
        protocol_version=sidecar.protocolVersion,
        file=file,
        sidecar=sidecar,
        instructions=instructions,
        source=source,
        catalog_file_path=catalog_file_path or (root_path / "catalog.json"),
        install_checksum=install_checksum,
        root_path=root_path,
        checksum=checksum,
        validators=compiled_validators(checksum, file),
        name=name,
    )


class CatalogRegistry:
    """Live catalog lookup: builtins (loaded once) plus CACHED pack discovery.

    See the module docstring for the cache doctrine. ``.invalidate()`` is the
    ONLY way the pack-discovery cache clears; nothing here re-scans the
    filesystem on a lookup.
    """

    def __init__(self) -> None:
        from clio_agent.gact.a2ui_catalogs.builtin import load_builtin_catalogs  # noqa: PLC0415

        self._builtin: list[CatalogEntry] = list(load_builtin_catalogs())
        self._builtin_by_key: dict[tuple[str, str], CatalogEntry] = {
            (entry.catalog_id, entry.protocol_version): entry for entry in self._builtin
        }
        self._lock = threading.Lock()
        self._discovered_blueprints: list[Any] | None = None
        self._pack_cache: list[CatalogEntry] | None = None
        # Bounded PER SESSION (adversarial review: an unbounded per-session list
        # is a release-gating memory leak for a long-lived session -- same ring
        # size as the global ledger in ``reasons.py``, one source of truth).
        self._session_reasons: dict[str, "deque[dict[str, Any]]"] = {}
        self._session_reasons_lock = threading.Lock()
        # S5b: event NAMES this session has already recorded
        # ``a2ui_event_narration_undeclared`` for, so a chatty client
        # resubmitting the same undeclared event doesn't flood the ledger
        # with a duplicate reason on every action (unlike
        # ``a2ui_event_destination_undeclared``, recorded per-action).
        # Bounded per session at the SAME ring size as ``_session_reasons``
        # for one source of truth on "how many": past that, further distinct
        # undeclared names simply stop deduping (still recorded, just no
        # longer once-only) rather than growing unbounded.
        self._narration_undeclared_seen: dict[str, set[str]] = {}

    def record_session_reason(self, session_id: str, reason: str, **fields: Any) -> dict[str, Any]:
        """Record a typed catalog reason AND append it to ``session_id``'s ledger.

        The ONE recorder both production doors (the HTTP route and the
        ``create_a2ui_surface`` tool) call, so a session's catalog-boundary
        history is retrievable regardless of which door produced it
        (adversarial S2 review — no new ``app.state`` store: this rides the
        existing ``app.state.a2ui_catalogs`` instance).
        """

        from clio_agent.gact.a2ui_catalogs.reasons import (  # noqa: PLC0415
            A2UI_CATALOG_REASON_RING_MAXLEN,
            record_a2ui_catalog_reason,
        )

        row = record_a2ui_catalog_reason(reason, session_id=session_id, **fields)
        with self._session_reasons_lock:
            ring = self._session_reasons.setdefault(
                session_id, deque(maxlen=A2UI_CATALOG_REASON_RING_MAXLEN)
            )
            ring.append(row)
        return row

    def record_narration_undeclared_once(self, session_id: str, event_name: str) -> bool:
        """Record ``a2ui_event_narration_undeclared`` for ``event_name``, once.

        Dedupes on ``(session_id, event_name)`` via ``_narration_undeclared_
        seen`` -- a repeat submission of the SAME undeclared event name
        (a2ui_actions/dispatcher.py's per-action idempotency dedupe already
        catches an exact resubmission; this catches distinct actions sharing
        one undeclared name) records the typed reason once, not once per
        action, per the S5b campaign deliverable.

        Returns:
            ``True`` iff this call newly recorded the reason (first time this
            session has seen ``event_name`` go undeclared); ``False`` when
            already recorded.
        """

        from clio_agent.gact.a2ui_catalogs.reasons import (  # noqa: PLC0415
            A2UI_CATALOG_REASON_RING_MAXLEN,
        )

        with self._session_reasons_lock:
            seen = self._narration_undeclared_seen.setdefault(session_id, set())
            if event_name in seen:
                return False
            if len(seen) < A2UI_CATALOG_REASON_RING_MAXLEN:
                seen.add(event_name)
        self.record_session_reason(session_id, "a2ui_event_narration_undeclared", action=event_name)
        return True

    def session_reasons(self, session_id: str) -> list[dict[str, Any]]:
        """Return the typed catalog reasons recorded for ``session_id``, oldest first."""

        with self._session_reasons_lock:
            return list(self._session_reasons.get(session_id, []))

    def invalidate(self) -> None:
        """Drop the cached discovery + pack-catalog list.

        Called by ``routes/blueprints.py``'s install/update/uninstall
        handlers after a mutation completes; the next lookup re-discovers
        once and repopulates both caches.
        """

        with self._lock:
            self._discovered_blueprints = None
            self._pack_cache = None

    def discovered_blueprints(self) -> list[Any]:
        """Return the cached ``discover_agent_blueprints()`` result.

        Shared by this registry's own pack-catalog loading AND
        ``activation._active_blueprint``'s no-path-active branch, so a
        session resolving its active blueprint and this registry resolving
        pack catalogs never each trigger their own independent filesystem
        scan for the same lookup.
        """

        with self._lock:
            if self._discovered_blueprints is not None:
                return self._discovered_blueprints
        from clio_agent.gact.a2ui_catalogs.reasons import (  # noqa: PLC0415
            record_a2ui_catalog_reason,
        )
        from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415

        try:
            blueprints = discover_agent_blueprints()
        except Exception as exc:  # noqa: BLE001 - typed, recorded, never silent
            record_a2ui_catalog_reason("a2ui_blueprint_discovery_failed", detail=str(exc))
            blueprints = []
        with self._lock:
            self._discovered_blueprints = blueprints
            return blueprints

    def _packs(self) -> list[CatalogEntry]:
        with self._lock:
            if self._pack_cache is not None:
                return self._pack_cache
        from clio_agent.gact.a2ui_catalogs.blueprint import (  # noqa: PLC0415
            load_all_blueprint_catalogs,
        )

        entries = load_all_blueprint_catalogs(self.discovered_blueprints())
        with self._lock:
            self._pack_cache = entries
            return entries

    def builtin(self) -> list[CatalogEntry]:
        """Return the two builtin catalogs (Basic, CLIO workspace)."""

        return list(self._builtin)

    def installed(self) -> list[CatalogEntry]:
        """Return every catalog this server can validate against: builtin ∪ packs."""

        return [*self._builtin, *self._packs()]

    def get(self, catalog_id: str, protocol_version: str = A2UI_V091) -> CatalogEntry | None:
        """Return the installed entry for ``(catalog_id, protocol_version)``, or ``None``.

        Checks the builtin catalogs first with zero I/O; only a miss there
        touches the (cached) pack-discovery path.
        """

        if not catalog_id:
            return None
        builtin_hit = self._builtin_by_key.get((catalog_id, protocol_version))
        if builtin_hit is not None:
            return builtin_hit
        for entry in self._packs():
            if entry.catalog_id == catalog_id and entry.protocol_version == protocol_version:
                return entry
        return None


__all__ = [
    "CatalogEntry",
    "CatalogRegistry",
    "CatalogResolver",
    "CatalogSource",
    "compiled_validators",
    "make_entry",
]
