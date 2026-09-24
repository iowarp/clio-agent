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
        # S8 review round (issue #1374 item B): a cheap integer callers can
        # compare against to know "has the installed-catalog set possibly
        # changed" without re-walking anything -- A2UIStore's projection
        # cache uses this to invalidate itself when a pack install/uninstall
        # could change how an ALREADY-persisted part folds (e.g.
        # state="unknown" -> resolved), a change no session message ever
        # reflects on its own.
        self._generation = 0
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
        # S8 (issue #1374 live-gate comment): ONE (turn_id, reason -> count)
        # slot per session -- only the CURRENT turn's counts matter, so a new
        # turn_id for a session drops the prior turn's counts instead of
        # accumulating across the session's whole lifetime (bounded memory
        # is release-gating; same doctrine as ``_narration_undeclared_seen``).
        self._producer_refusal_state: dict[str, tuple[str, dict[str, int]]] = {}
        # v15 S8: keys ``record_session_reason_once`` already recorded, per session.
        self._session_once_keys: dict[str, set[tuple[Any, ...]]] = {}

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

    def record_session_reason_once(
        self, session_id: str, reason: str, *, key: tuple[Any, ...] = (), **fields: Any
    ) -> bool:
        """Record ``reason`` for ``session_id`` once per ``(reason, *key)``.

        For a condition re-derived on every call (the session's catalog
        resolution runs per request and per turn build), so the session
        ledger, the audit stream and the log carry it once instead of once per
        call. Bounded per session at the ledger's ring size; past that, further
        distinct keys are still recorded, just no longer deduplicated.

        Returns:
            ``True`` iff this call recorded the reason.
        """

        from clio_agent.gact.a2ui_catalogs.reasons import (  # noqa: PLC0415
            A2UI_CATALOG_REASON_RING_MAXLEN,
        )

        once_key = (reason, *key)
        with self._session_reasons_lock:
            seen = self._session_once_keys.setdefault(session_id, set())
            if once_key in seen:
                return False
            if len(seen) < A2UI_CATALOG_REASON_RING_MAXLEN:
                seen.add(once_key)
        self.record_session_reason(session_id, reason, **fields)
        return True

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

    def record_producer_refusal_reason(self, session_id: str, turn_id: str, reason: str) -> bool:
        """Record one producer-tool refusal ``reason`` for ``(session_id, turn_id)``.

        A SECOND (or later) occurrence of the SAME ``reason`` within the SAME
        turn additionally records the typed ``a2ui_producer_refusal_repeated``
        ledger reason -- observability only, never a cap or a reroute (⚑ #1:
        clio never decides FOR the model). Evidence this exists for: a resumed
        idle turn (claude_code/sonnet, 2026-09-17) called
        ``create_a2ui_surface`` and got ``a2ui_client_capabilities_unknown``
        14 times in a row, invisible without hand-reading the semantic trace
        (issue #1374 live-gate comment).

        ``turn_id`` empty (``gact/context.py``'s ``TurnContext.turn_id``
        default -- a producer tool called with no active turn, e.g. a script
        or a test harness) is never a real grouping key: two out-of-turn
        calls have no actual "same turn" relationship, so this is a no-op
        rather than silently treating them as one ever-growing turn (S8
        review nit, issue #1374).

        Returns:
            ``True`` iff this call recorded a repeat (this session's second+
            occurrence of ``reason`` within ``turn_id``); ``False`` on the
            first occurrence of a reason within a turn, when a new
            ``turn_id`` resets this session's counts, or when ``turn_id`` is
            empty.
        """

        if not turn_id:
            return False
        with self._session_reasons_lock:
            state = self._producer_refusal_state.get(session_id)
            if state is None or state[0] != turn_id:
                state = (turn_id, {})
                self._producer_refusal_state[session_id] = state
            counts = state[1]
            count = counts.get(reason, 0) + 1
            counts[reason] = count
        if count > 1:
            self.record_session_reason(
                session_id,
                "a2ui_producer_refusal_repeated",
                refusal_reason=reason,
                count=count,
            )
            return True
        return False

    def session_reasons(self, session_id: str) -> list[dict[str, Any]]:
        """Return the typed catalog reasons recorded for ``session_id``, oldest first."""

        with self._session_reasons_lock:
            return list(self._session_reasons.get(session_id, []))

    def forget_session(self, session_id: str) -> None:
        """Drop every per-session ring this registry keeps for ``session_id``.

        Nit (S8 review round, issue #1374): ``DELETE /v1/sessions/{sid}``
        never pruned ``_session_reasons``/``_narration_undeclared_seen``/
        ``_producer_refusal_state`` — a bounded-per-session leak (each ring
        is capped, but the DICT of rings itself grows by one entry per
        session ever created, never shrinking) that outlives the session
        for the rest of the process. Called from the session-delete route.
        """

        with self._session_reasons_lock:
            self._session_reasons.pop(session_id, None)
            self._narration_undeclared_seen.pop(session_id, None)
            self._producer_refusal_state.pop(session_id, None)
            self._session_once_keys.pop(session_id, None)

    def invalidate(self) -> None:
        """Drop the cached discovery + pack-catalog list.

        Called by ``routes/blueprints.py``'s install/update/uninstall
        handlers after a mutation completes; the next lookup re-discovers
        once and repopulates both caches. Also bumps :attr:`generation`.
        """

        with self._lock:
            self._discovered_blueprints = None
            self._pack_cache = None
            self._generation += 1

    @property
    def generation(self) -> int:
        """Bumped by every :meth:`invalidate` call (S8, issue #1374)."""

        return self._generation

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
