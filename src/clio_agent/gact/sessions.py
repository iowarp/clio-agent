"""Stable GACT session addresses and presentation/configuration metadata.

Tasks, assignment history, mail and input commitments live in the confirmed Core
hierarchy. This thread-safe store supplies session metadata and trusted startup
receipts. Each session is one small row in ``state/server/sessions.sqlite3``
(:mod:`clio_agent.gact.session_records`), imported once from a legacy
``sessions.json``; ``CLIO_SESSIONS_PATH`` overrides the ``sessions.json`` path the
table sits beside. Rows load on first use into a bounded cache, so neither boot nor
memory grows with the number of conversations. Public wire fields are projected from
the record; private approval and initialization fields never enter public updates.
"""

from __future__ import annotations

import asyncio
import builtins
import copy
import hashlib
import json
import logging
import threading
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, get_args

from clio_agent.gact.message_marks import merge_session_metadata
from clio_agent.gact.session_records import (
    ROW_UNREADABLE_REASON,
    PageQuery,
    SessionRecords,
    cache_capacity,
    record_columns,
)
from clio_agent.gact.types import Session as _WireSession
from clio_agent.runtime.stream_audit import stream_audit

logger = logging.getLogger(__name__)

# Keep session ids namespaced so log scraping (and humans) can tell
# them apart from e.g. message ids at a glance.
_SESSION_ID_PREFIX = "sess_"
#: Server-owned approval profiles (never accepted from a public request): the SPOTTER
#: watcher's containment profile and the read-only side session (gact/side_sessions.py).
_SERVER_PROFILES = frozenset({"spotter-watcher", "read-only-side"})
_TIME_LOCK = threading.Lock()
_LAST_TIME: datetime | None = None


def _require_creation_worker() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Trusted session initialization must run off the event loop")


def _utcnow_iso() -> str:
    """Return the current UTC time in ISO-8601 (microsecond precision).

    Microsecond precision matters for ordering: if two sessions are
    created within the same second (common in tests + batch imports),
    second-precision strings tie and ``list()`` can't sort them
    deterministically. Microseconds effectively never tie in single-
    process code.
    """

    global _LAST_TIME
    with _TIME_LOCK:
        now = datetime.now(timezone.utc)
        if _LAST_TIME is not None and now <= _LAST_TIME:
            now = _LAST_TIME + timedelta(microseconds=1)
        _LAST_TIME = now
        return now.isoformat()


def _default_store_path() -> Path:
    """Resolve server state, preserving an existing legacy store until migration."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    override = conf.resolve(
        "paths.sessions", env="CLIO_SESSIONS_PATH", default="", cast=conf.as_str
    ).strip()
    if override:
        return Path(override).expanduser()
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    legacy = Path.cwd() / ".clio" / "agent" / "sessions.json"
    if legacy.is_file():
        logger.warning("Using legacy sessions at %s; migrate-paths relocates this store", legacy)
        return legacy
    return paths.server_state_dir() / "sessions.json"


#: Modes this record may hold on disk that the wire model no longer accepts,
#: mapped to the mode they are equivalent to.
#:
#: ``chat`` was deleted by P1.1 (#1063). That change is documented as
#: behaviour-preserving — "nothing ever checked mode==\"chat\", so it behaved
#: identically to ``edit``" — so this mapping restates an existing ruling
#: rather than inventing one. Every session written before #1063 carries it.
RETIRED_MODES: dict[str, str] = {"chat": "edit"}

#: Where an unrecognised mode lands. ``plan`` is the read-only mode: an
#: unknown value must never be resolved into MORE authority than it had, and
#: guessing ``edit`` would hand write access to a record we cannot interpret.
UNKNOWN_MODE_FALLBACK = "plan"

#: Modes the wire model accepts (clio_agent.gact.types.Session).
_WIRE_MODES = frozenset({"plan", "edit", "architect"})


def normalize_stored_mode(mode: str) -> str:
    """Map a stored mode onto one the wire model accepts.

    The write side stopped accepting some values without migrating what was
    already on disk (#1171). Reading must therefore tolerate what writing no
    longer produces, or every pre-existing session becomes unreadable.
    """

    if mode in _WIRE_MODES:
        return mode
    return RETIRED_MODES.get(mode, UNKNOWN_MODE_FALLBACK)


#: Statuses the wire model accepts (``clio_agent.gact.types.Session.status``),
#: read off that Literal so the two cannot drift apart.
SESSION_STATUSES: frozenset[str] = frozenset(
    get_args(_WireSession.model_fields["status"].annotation)
)

#: Where an unreadable stored status lands. A status we cannot interpret is not
#: evidence of health, so it resolves to ``error`` rather than to ``idle`` --
#: the same "never resolve into MORE authority" rule ``UNKNOWN_MODE_FALLBACK``
#: applies to modes.
UNKNOWN_STATUS_FALLBACK = "error"


def normalize_stored_status(status: str) -> str:
    """Map a stored status onto one the wire model accepts.

    ``SessionStore`` rows are a plain dataclass with ``status: str``, so nothing
    validated the value on the way in and a store written by an older (or
    buggier) build can hold anything. ``ListSessionsResponse`` DOES validate, so
    one unreadable row would 500 the whole listing for every other session.
    """

    return status if status in SESSION_STATUSES else UNKNOWN_STATUS_FALLBACK


@dataclass
class Session:
    """A single GACT v0.2 session record.

    Kept as a plain dataclass (not a Pydantic model) because the
    registry needs to serialise to JSON trivially and we control the
    shape explicitly — no validation needed at this layer.
    """

    id: str
    workspace_id: str
    title: str
    status: str = "idle"
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    last_interaction_at: str = ""
    message_count: int = 0
    parent_session_id: str = ""
    model: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=lambda: {"id": "main"})
    # cumulative token + cost rollup. Populated
    # from Prediction.tokens / cost_usd on every turn.
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    # True once any turn accumulated onto cost_usd with a REAL cost source
    # (provider report or a price-table match). False means no turn has ever
    # produced a known cost for this session -- the wire projection serializes
    # cost_usd as null rather than an indistinguishable $0.00 (#775).
    cost_known: bool = False
    # The same rollup per ``account/model``: ``{ref: {tokens_input, tokens_output,
    # cost_usd, turns}}`` (the fleet's spend; docs/design/sdk/model-fleet.md).
    usage_by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    # iowarp/clio-agent — capabilities.plan_mode + edit_modes:
    # mode controls what the agent can do; edit_mode controls how
    # it proposes changes when it can. Default {edit, diff}.
    #
    #   plan       — read-only; permission gate denies any destructive op
    #                (built-in plan_acl rules), except the sole <plans>/*.md write
    #   edit       — full read+write authority; agent can modify files
    #   architect  — high-level plan + diff proposals; no direct file writes
    #
    # P1.1 #1063 deleted the ``chat`` mode: nothing ever checked mode=="chat", so
    # it behaved identically to ``edit`` — its removal is behavior-preserving and
    # the default moved to ``edit``.
    mode: str = "edit"
    edit_mode: str = "diff"
    # iowarp/clio-agent #1034 — approval axis, ORTHOGONAL to ``mode``. One of
    # {ask, auto-edits, bypass, ai-review, spotter-ai}; default "ask" preserves
    # today's interactive-prompt behaviour. Literal validation lives on the wire model
    # (types.Session); the dataclass stores the raw string so a defaulted field
    # round-trips old persisted rows (asdict/Session(**payload)) with no
    # migration.
    approval_mode: str = "ask"
    # Server-owned approval refinement for internal executors. This field is
    # persisted so a standing child retains its authority after restart, but
    # ``to_wire`` removes it and no public create/update request accepts it.
    # The public approval mode therefore remains truthful while the dedicated
    # SPOTTER watcher can contain an anomaly without acquiring blanket bypass.
    approval_profile: str = ""
    # Trusted child creation identity; immutable through public session updates.
    # It names creation metadata, never an execution/retry policy.
    creation_digest: str = ""
    creation_lifecycle_confirmed: bool = False
    # A missing legacy receipt is unknown, never permission to replay a hook.
    creation_hook_state: str = "unknown"
    # Routing override. "auto" runs the LM-based router; "chat" forces
    # every turn through the chat path; "experts" rejects chat/none
    # routes (raises a routing error) so users can lock the session
    # to data/analysis/visualization work. Default "auto" preserves
    # historical behaviour.
    routing_mode: str = "auto"
    metadata: dict[str, Any] = field(default_factory=dict)
    # iowarp/gact-tui §audit/E-14: archive bucket toggle. Sessions with
    # archived=True drop out of the active list (GET /v1/sessions
    # defaults to archived=False) but stay browsable via
    # GET /v1/sessions?archived=true. Pin / fork-lineage / autorename
    # state lives in `metadata`.
    archived: bool = False

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable dict matching SPEC §4.2's optional-
        field-friendly shape (no nulls where the client expects
        empty)."""

        wire = asdict(self)
        wire["mode"] = normalize_stored_mode(self.mode)
        wire["status"] = normalize_stored_status(self.status)
        wire.pop("approval_profile", None)
        wire.pop("creation_digest", None)
        wire.pop("creation_lifecycle_confirmed", None)
        wire.pop("creation_hook_state", None)
        wire.pop("usage_by_model", None)
        # cost_known is bookkeeping, not part of the wire contract -- it
        # decides whether cost_usd is a real number or null, never rides
        # along itself (#775: no turn ever reporting a cost must read as
        # unknown here too, matching the v3 projection's cost_known check).
        if not wire.pop("cost_known", False):
            wire["cost_usd"] = None
        return wire


class SessionStore:
    """Thread-safe session registry over one row per session.

    ``path=None`` keeps the table in memory (tests); ``path=Path`` names the legacy
    ``sessions.json`` the ``sessions.sqlite3`` table sits beside. Every mutation
    writes only the changed session's row, under one lock, before returning.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._records = SessionRecords(
            None if path is None else path.with_name(path.stem + ".sqlite3"),
            legacy_json=path,
        )
        # Rows load on first use. The LRU bounds what stays resident; the weak map
        # keeps one object per session while any caller still holds it, so an
        # update is never applied to a stale copy of a row someone is reading.
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._identity: weakref.WeakValueDictionary[str, Session] = weakref.WeakValueDictionary()
        self._cache_cap = cache_capacity()
        self._lifecycle_observer: Callable[[str, Session], None] | None = None
        self._initializing: set[str] = set()

    # ---- lifecycle ----------------------------------------------------

    def set_lifecycle_observer(self, observer: Callable[[str, Session], None] | None) -> None:
        """Install the app-owned semantic lifecycle bridge."""
        self._lifecycle_observer = observer

    def _observe_lifecycle(self, event_type: str, session: Session) -> None:
        # The lifecycle record is clio-core's: a failure is the caller's error.
        observer = self._lifecycle_observer
        if observer is not None:
            observer(event_type, session)

    # ---- rows (caller holds ``self._lock``) -----------------------------

    def _decode(self, sid: str, body: str) -> Optional[Session]:
        try:
            return Session(**json.loads(body))
        except (TypeError, ValueError) as exc:
            # Schema drift (a field renamed between releases): this one row is
            # unreadable; every other session keeps working.
            stream_audit(
                ROW_UNREADABLE_REASON,
                reason=ROW_UNREADABLE_REASON,
                session_id=sid,
                error=type(exc).__name__,
            )
            return None

    def _remember(self, session: Session) -> Session:
        self._identity[session.id] = session
        self._cache[session.id] = session
        self._cache.move_to_end(session.id)
        while len(self._cache) > self._cache_cap:
            self._cache.popitem(last=False)
        return session

    def _resolve(self, sid: str, body: str) -> Optional[Session]:
        held = self._cache.get(sid) or self._identity.get(sid)
        if held is not None:
            return held
        session = self._decode(sid, body)
        if session is not None:
            self._identity[sid] = session
        return session

    def _row(self, sid: str) -> Optional[Session]:
        held = self._cache.get(sid) or self._identity.get(sid)
        if held is not None:
            return self._remember(held)
        body = self._records.body(sid)
        session = None if body is None else self._decode(sid, body)
        return None if session is None else self._remember(session)

    def _persist(self, session: Session) -> None:
        """Write this one session's row; on failure the next read reloads the table."""
        try:
            self._records.put(record_columns(session), json.dumps(asdict(session), sort_keys=True))
        except BaseException:
            self._forget(session.id)
            raise
        self._remember(session)

    def _forget(self, sid: str) -> None:
        self._cache.pop(sid, None)
        self._identity.pop(sid, None)

    # ---- CRUD ---------------------------------------------------------

    def create(
        self,
        *,
        workspace_id: str,
        title: str = "",
        metadata: Optional[dict[str, Any]] = None,
        parent_session_id: str = "",
        model: Optional[dict[str, str]] = None,
        agent: Optional[dict[str, str]] = None,
        mode: str = "edit",
        edit_mode: str = "diff",
        routing_mode: str = "auto",
        approval_mode: str = "ask",
        approval_profile: str = "",
        creation_id: str = "",
        creation_digest: str = "",
        defer_initialization: bool = False,
    ) -> Session:
        """Create a new session. Returns the freshly-minted record.

        ``routing_mode`` mirrors the same validation as ``update()`` so a
        client can pre-lock a fresh session to a non-default routing mode
        (e.g. issue #25's ``reasoning_only``) without an extra PATCH.

        Trusted conversation owners may supply a stable creation identity and
        request digest. A matching existing child is returned without replaying
        completed initialization or replacing human-edited settings. Known
        unfinished initialization resumes; an admitted hook without a confirmed
        receipt reports uncertainty. Trusted children defer startup to their
        session-owned worker; preparing a row never starts model execution.
        """

        if bool(creation_id) != bool(creation_digest) or (
            creation_digest
            and (
                len(creation_digest) != 64
                or any(c not in "0123456789abcdef" for c in creation_digest)
            )
        ):
            raise ValueError("Reusable session creation requires its trusted identity and digest")
        if defer_initialization and not creation_id:
            raise ValueError("Deferred initialization requires trusted session creation")
        if creation_id and not defer_initialization:
            _require_creation_worker()
        sid = _SESSION_ID_PREFIX + (
            hashlib.sha256(creation_id.encode()).hexdigest()[:32]
            if creation_id
            else uuid.uuid4().hex[:12]
        )
        now = _utcnow_iso()
        valid_routing_modes = {"auto", "chat", "experts", "reasoning_only"}
        valid_approval_modes = {"ask", "auto-edits", "bypass", "ai-review", "spotter-ai"}
        sess = Session(
            id=sid,
            workspace_id=workspace_id,
            title=title or f"session {sid[-6:]}",
            status="idle",
            created_at=now,
            updated_at=now,
            last_interaction_at=now,
            metadata=dict(metadata or {}),
            parent_session_id=parent_session_id,
            model=dict(model or {}),
            agent=dict(agent or {"id": "main"}),
            mode=mode if mode in {"plan", "edit", "architect"} else "edit",
            edit_mode=edit_mode if edit_mode in {"diff", "whole", "patch"} else "diff",
            routing_mode=routing_mode if routing_mode in valid_routing_modes else "auto",
            approval_mode=approval_mode if approval_mode in valid_approval_modes else "ask",
            approval_profile=(approval_profile if approval_profile in _SERVER_PROFILES else ""),
            creation_digest=creation_digest,
            creation_hook_state="pending" if creation_id else "unknown",
        )
        with self._lock:
            existing = self._row(sid)
            if existing is not None:
                if (
                    not creation_id
                    or existing.creation_digest != creation_digest
                    or existing.workspace_id != workspace_id
                    or existing.parent_session_id != parent_session_id
                    or existing.agent != sess.agent
                    or existing.approval_profile != sess.approval_profile
                ):
                    raise ValueError("Session creation identity was reused with different work")
                # Reconfirm the original address, then resume only initialization
                # that is known unfinished. Completed work is never repeated.
                self._persist(existing)
                sess = existing
            else:
                self._persist(sess)
        if defer_initialization:
            return sess
        if creation_id:
            return self.initialize(sid)
        return self._finish_creation(sess)

    def initialize(self, sid: str, *, cancel_event: threading.Event | None = None) -> Session:
        """Initialize a prepared child on its exclusive worker, without holding task locks.

        An unconfirmed admitted hook is never replayed. Concurrent initialization
        fails immediately instead of blocking a shared worker or repeating effects.
        """
        _require_creation_worker()
        with self._lock:
            sess = self._row(sid)
            if sess is None:
                raise ValueError("Prepared session is unavailable")
            if not sess.creation_digest:
                return sess
            if sess.creation_hook_state in {"completed", "interrupted"}:
                if not sess.creation_lifecycle_confirmed:
                    raise ValueError("Session initialization receipts are inconsistent")
                return sess
            if sid in self._initializing:
                raise RuntimeError("Session initialization is already running")
            self._initializing.add(sid)
        try:
            return self._finish_creation(sess, cancel_event=cancel_event)
        finally:
            with self._lock:
                self._initializing.discard(sid)

    def _finish_creation(
        self, sess: Session, *, cancel_event: threading.Event | None = None
    ) -> Session:
        sid, trusted = sess.id, bool(sess.creation_digest)
        from clio_agent.gact.hooks import dispatch_session_start  # noqa: PLC0415
        from clio_agent.gact.hooks.wire import HookCancelled  # noqa: PLC0415

        if cancel_event is not None and cancel_event.is_set():
            raise HookCancelled("session-start")
        try:
            if not trusted or not sess.creation_lifecycle_confirmed:
                if trusted and self._lifecycle_observer is None:
                    raise RuntimeError("Trusted session creation requires its lifecycle observer")
                self._observe_lifecycle("session.created", sess)
                if trusted:
                    with self._lock:
                        sess.creation_lifecycle_confirmed = True
                        self._persist(sess)
        except BaseException:
            # Trusted task creation can already have a confirmed Core event
            # whose reply was lost. Retain its stable address for reconciliation.
            if trusted:
                raise
            # No session exists that clio-core has no record of.
            with self._lock:
                self._records.delete(sid)
                self._forget(sid)
            raise
        # Reuse known-unattempted initialization. An admitted hook with no
        # completion receipt is uncertain and must never be replayed by reuse.
        if cancel_event is not None and cancel_event.is_set():
            raise HookCancelled("session-start")
        if trusted:
            with self._lock:
                if sess.creation_hook_state != "pending":
                    raise RuntimeError(
                        f"SessionStart effects are unconfirmed for {sid}; inspect before retrying"
                    )
                sess.creation_hook_state = "started"
                try:
                    self._persist(sess)
                except BaseException:
                    # This process knows the hook was never called. A restart
                    # seeing a persisted 'started' receipt remains uncertain.
                    sess.creation_hook_state = "pending"
                    raise
        try:
            dispatch_session_start(
                session_id=sid,
                cancel_event=cancel_event,
                payload={
                    "workspace_id": sess.workspace_id,
                    "parent_session_id": sess.parent_session_id,
                    "agent": dict(sess.agent),
                    "mode": sess.mode,
                },
            )
        except HookCancelled as error:
            if trusted and error.cleanup_confirmed:
                with self._lock:
                    sess.creation_hook_state = (
                        "pending" if error.invocation_started is False else "interrupted"
                    )
                    self._persist(sess)
            raise
        if trusted:
            with self._lock:
                sess.creation_hook_state = "completed"
                self._persist(sess)
        return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._row(sid)

    def snapshot(self, sid: str) -> Session | None:
        """Return an independent, atomically captured metadata row for preparation."""
        with self._lock:
            session = self._row(sid)
            return copy.deepcopy(session) if session is not None else None

    def startup_retirement_error(self, sid: str) -> str | None:
        """Report missing physical proof; unknown effects alone do not imply a live process."""
        with self._lock:
            session = self._row(sid)
            if session is None:
                return "Session startup evidence is unavailable; settlement remains unconfirmed."
            if session.creation_digest and session.creation_hook_state == "started":
                return "SessionStart process settlement is unconfirmed; no replay or successor was launched."
        return None

    def list(
        self,
        *,
        workspace_id: Optional[str] = None,
    ) -> list[Session]:
        """List every session, newest created first. Optional workspace filter.

        Reads every row of the table: for a page of the conversation list use
        :meth:`page`, for ids alone :meth:`ids`.
        """

        with self._lock:
            pairs = self._records.bodies(workspace_id=workspace_id)
            rows = [self._resolve(sid, body) for sid, body in pairs]
        return [row for row in rows if row is not None]

    def page(self, query: PageQuery) -> tuple["builtins.list[Session]", Optional[str], int]:
        """One page of the conversation list, newest interaction first.

        Returns the rows, the cursor of the next page (``None`` on the last page)
        and how many rows match the filters in all. Raises
        :class:`~clio_agent.gact.session_records.InvalidCursorError` for a cursor
        this server did not issue.
        """

        with self._lock:
            pairs, next_cursor, total = self._records.page(query)
            rows = [self._resolve(sid, body) for sid, body in pairs]
        return [row for row in rows if row is not None], next_cursor, total

    def ids(self) -> "builtins.list[str]":
        """Every session id, without reading a row."""

        with self._lock:
            return self._records.ids()

    def child_ids_by_parent(self) -> dict[str, "builtins.list[str]"]:
        """``{parent id: [child ids]}`` from the parent column, without reading rows."""

        with self._lock:
            return self._records.child_ids_by_parent()

    def most_recent(self) -> Optional[Session]:
        """The newest created session, or ``None``."""

        with self._lock:
            sid = self._records.most_recent_id()
            return None if sid is None else self._row(sid)

    def delete(self, sid: str) -> bool:
        with self._lock:
            existing = self._row(sid)
            existed = existing is not None
            if existed:
                self._records.delete(sid)
            self._forget(sid)
        if existed:
            # P2.3 SessionEnd lifecycle hook (observation): fires exactly once, only
            # when a session actually existed and was removed.
            from clio_agent.gact.hooks import dispatch_session_end  # noqa: PLC0415

            dispatch_session_end(session_id=sid)
            assert existing is not None
            self._observe_lifecycle("session.deleted", existing)
        return existed

    def update(
        self,
        sid: str,
        *,
        title: Optional[str] = None,
        status: Optional[str] = None,
        message_count: Optional[int] = None,
        add_tokens_input: int = 0,
        add_tokens_output: int = 0,
        add_cost_usd: Optional[float] = None,
        usage_totals: tuple[int, int, float, bool] | None = None,
        usage_by_model: Optional[dict[str, dict[str, Any]]] = None,
        mode: Optional[str] = None,
        edit_mode: Optional[str] = None,
        routing_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        approval_profile: Optional[str] = None,
        model: Optional[dict[str, str]] = None,
        agent: Optional[dict[str, str]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
        archived: Optional[bool] = None,
    ) -> Optional[Session]:
        """Mutate a session in place.

        Any field left ``None`` is untouched. ``metadata_patch``
        merges into existing metadata; message marks merge per ID,
        other keys shallowly, without clobbering the rest. The
        ``add_tokens_*`` / ``add_cost_usd`` params accumulate onto
        the session rollup — pass a turn's numbers after each
        forward() call. ``add_cost_usd=None`` (the default) means "no cost
        source for this turn" and leaves ``cost_usd``/``cost_known``
        untouched; pass a real float (0.0 included) only when the turn
        actually produced one, which is what flips ``cost_known`` True.
        ``usage_totals`` replaces that projection with fixed totals, allowing a
        finalized turn to retry a failed flush without accumulating twice.
        """

        with self._lock:
            sess = self._row(sid)
            if sess is None:
                return None
            if approval_profile is not None:
                if approval_profile and approval_profile not in _SERVER_PROFILES:
                    raise ValueError("Unknown server-owned approval profile")
                sess.approval_profile = approval_profile
            if title is not None:
                sess.title = title
            if status is not None:
                # Every other closed vocabulary below is guarded; ``status`` was
                # not, and a dataclass field takes anything. An invented status
                # persists silently and only detonates at the wire boundary,
                # where it 500s the listing for every OTHER session. Refuse it
                # here -- loudly, because the caller is server code with a bug,
                # not a client sending bad input.
                if status not in SESSION_STATUSES:
                    raise ValueError(
                        f"session status {status!r} is outside the wire vocabulary "
                        f"{sorted(SESSION_STATUSES)}"
                    )
                sess.status = status
            if message_count is not None:
                sess.message_count = message_count
            if add_tokens_input:
                sess.tokens_input += add_tokens_input
            if add_tokens_output:
                sess.tokens_output += add_tokens_output
            if add_cost_usd is not None:
                sess.cost_usd += add_cost_usd
                sess.cost_known = True
            if usage_totals is not None:
                sess.tokens_input, sess.tokens_output, sess.cost_usd, sess.cost_known = usage_totals
            if usage_by_model is not None:
                sess.usage_by_model = {ref: dict(row) for ref, row in usage_by_model.items()}
            if mode is not None and mode in {"plan", "edit", "architect"}:
                sess.mode = mode
            if edit_mode is not None and edit_mode in {"diff", "whole", "patch"}:
                sess.edit_mode = edit_mode
            if routing_mode is not None and routing_mode in {
                "auto",
                "chat",
                "experts",
                "reasoning_only",
            }:
                sess.routing_mode = routing_mode
            if approval_mode is not None and approval_mode in {
                "ask",
                "auto-edits",
                "bypass",
                "ai-review",
                "spotter-ai",
            }:
                sess.approval_mode = approval_mode
            if model is not None:
                sess.model = dict(model)
            if agent is not None:
                sess.agent = dict(agent)
            if metadata_patch is not None:
                # Readers retain Session rows after get() releases the store lock.
                # Publish a new mapping so an in-flight wire projection can finish
                # iterating its old metadata while task/mail writers patch the row.
                sess.metadata = merge_session_metadata(sess.metadata, metadata_patch)
            if archived is not None:
                sess.archived = bool(archived)
            now = _utcnow_iso()
            if message_count is not None:
                sess.last_interaction_at = now
            sess.updated_at = now
            self._persist(sess)
            return sess

    def sessions_without_interaction_time(self) -> "builtins.list[Session]":
        """Rows that predate ``last_interaction_at`` and still lack it.

        The transcript store answers when each was last used (the ``messages/`` file's
        mtime, or the last atom message with ``transcript.file`` off) and hands the
        answers to :meth:`settle_interaction_times`. Read from the indexed
        ``interaction_known`` column, so a store with none reads no row.
        """

        with self._lock:
            rows = [self._row(sid) for sid in self._records.without_interaction_time()]
        return [row for row in rows if row is not None and not row.last_interaction_at]

    def settle_interaction_times(self, times: Mapping[str, str]) -> None:
        """Record recovered interaction times for rows that still lack one.

        A row that gained a real interaction time meanwhile (a new message) keeps it.
        """

        with self._lock:
            for sid, when in times.items():
                session = self._row(sid)
                if session is not None and not session.last_interaction_at and when:
                    session.last_interaction_at = when
                    self._persist(session)

    # ---- introspection hooks (for /v1/memory/stats + tests) ----------

    def count(self) -> int:
        with self._lock:
            return self._records.count()

    def resident_count(self) -> int:
        """How many rows the bounded cache holds (memory evidence)."""
        with self._lock:
            return len(self._cache)
