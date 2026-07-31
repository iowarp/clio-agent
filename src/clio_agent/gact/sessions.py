"""GACT v0.2 session registry for CLIO.

Until session ownership moves into ARC, the GACT
app owns a small registry of ``Session`` records:

- in-memory dict keyed by session id
- optional JSON persistence so sessions survive ``clio-agent-gact``
  restarts (default: ``<cwd>/.clio/agent/sessions.json`` per
  :func:`_default_store_path`; ``CLIO_SESSIONS_PATH`` overrides the full path)

The registry is thread-safe for the workload we expect (FastAPI
serves requests concurrently but each request either reads or writes
the registry atomically; no coordinated multi-step state machines).

Session shape mirrors GACT v0.2 §4.2:

    {
      "id": "sess_...",
      "workspace_id": "ws_default",
      "title": "...",
      "status": "idle" | "running" | "waiting_permission" | "waiting_user" | "error",
      "created_at": "<ISO-8601 UTC>",
      "updated_at": "<ISO-8601 UTC>",
      "message_count": 0,
      "metadata": {...}
    }

Spec fields the scaffold doesn't populate yet (model, tokens,
cost_usd, summary, archived_at, parent_session_id, agent) stay at
their zero-values until their own items land. Clients tolerate
absent-optional fields per §3.2.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Keep session ids namespaced so log scraping (and humans) can tell
# them apart from e.g. message ids at a glance.
_SESSION_ID_PREFIX = "sess_"
_TIME_LOCK = threading.Lock()
_LAST_TIME: datetime | None = None


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
    """Default on-disk location for the registry: ``<cwd>/.clio/agent/sessions.json``.

    Per-workspace: the registry — and the messages / semantic traces / context-file
    metadata derived from its parent directory — all live under the workspace
    ``.clio/agent`` root alongside ARC. ``CLIO_SESSIONS_PATH`` overrides the full path.
    The directory is created lazily on first write.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    override = conf.resolve(
        "paths.sessions", env="CLIO_SESSIONS_PATH", default="", cast=conf.as_str
    ).strip()
    if override:
        return Path(override).expanduser()
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.workspace_agent_dir() / "sessions.json"


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
    message_count: int = 0
    parent_session_id: str = ""
    model: dict[str, str] = field(default_factory=dict)
    agent: dict[str, str] = field(default_factory=lambda: {"id": "main"})
    # cumulative token + cost rollup. Populated
    # from Prediction.tokens / cost_usd on every turn.
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
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
    # {ask, auto-edits, bypass, ai-review}; default "ask" preserves today's
    # interactive-prompt behaviour. Literal validation lives on the wire model
    # (types.Session); the dataclass stores the raw string so a defaulted field
    # round-trips old persisted rows (asdict/Session(**payload)) with no
    # migration.
    approval_mode: str = "ask"
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

        return asdict(self)


class SessionStore:
    """Thread-safe session registry with optional JSON persistence.

    Construction
    ------------

    ``path=None`` → purely in-memory (used in tests that don't care
    about persistence). ``path=Path`` → load on startup, write on
    every mutation.

    Concurrency
    -----------

    A single ``threading.Lock`` guards the in-memory dict. Every
    mutation writes to disk while holding the lock, so an interrupted
    write (process crash mid-flush) at worst loses the last
    un-committed change — it can't leave a half-written JSON file
    because we write+fsync to a temp file and atomically rename.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        if path is not None:
            self._load()
            # #1115: a PERSISTENT session registry is the durable home for in-flight
            # SEP-2663 task ids (RULE 4 - no fifth store). Publishing it here is what
            # lets the tools layer reach a durable store without importing gact; an
            # in-memory registry deliberately does not publish, because it would be
            # claiming a crash-recovery guarantee it cannot keep.
            from clio_agent.gact.mcp_task_store import (  # noqa: PLC0415 - keep leaf
                install_session_task_store,
            )

            install_session_task_store(self)

    # ---- lifecycle ----------------------------------------------------

    def _load(self) -> None:
        """Populate in-memory dict from the on-disk JSON, if any."""

        assert self._path is not None
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted / unreadable store — start fresh, don't
            # crash the server. The previous file stays on disk so
            # operators can salvage it out-of-band.
            return
        if not isinstance(raw, dict):
            return
        for sid, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            try:
                self._sessions[sid] = Session(**payload)
            except TypeError:
                # Schema drift (e.g. a field was renamed between
                # releases). Skip that one row and keep going.
                continue

    def _flush(self) -> None:
        """Serialise the in-memory dict to disk atomically.

        Caller holds ``self._lock``.
        """

        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {sid: asdict(s) for sid, s in self._sessions.items()}
        # write+fsync to a temp file, then atomic rename: the fsync forces the
        # bytes to disk before the rename publishes them, so a mid-write crash
        # can't leave a partial JSON blob on disk (temp-file + rename is atomic).
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

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
    ) -> Session:
        """Create a new session. Returns the freshly-minted record.

        ``routing_mode`` mirrors the same validation as ``update()`` so a
        client can pre-lock a fresh session to a non-default routing mode
        (e.g. issue #25's ``reasoning_only``) without an extra PATCH.
        """

        sid = _SESSION_ID_PREFIX + uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        valid_routing_modes = {"auto", "chat", "experts", "reasoning_only"}
        valid_approval_modes = {"ask", "auto-edits", "bypass", "ai-review"}
        sess = Session(
            id=sid,
            workspace_id=workspace_id,
            title=title or f"session {sid[-6:]}",
            status="idle",
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
            parent_session_id=parent_session_id,
            model=dict(model or {}),
            agent=dict(agent or {"id": "main"}),
            mode=mode if mode in {"plan", "edit", "architect"} else "edit",
            edit_mode=edit_mode if edit_mode in {"diff", "whole", "patch"} else "diff",
            routing_mode=routing_mode if routing_mode in valid_routing_modes else "auto",
            approval_mode=approval_mode if approval_mode in valid_approval_modes else "ask",
        )
        with self._lock:
            self._sessions[sid] = sess
            self._flush()
        # P2.3 SessionStart lifecycle hook (observation): fires exactly once per
        # created session, after it is persisted. Never blocks — the dispatcher
        # returns a no-op outcome when no hook is configured.
        from clio_agent.gact.hooks import dispatch_session_start  # noqa: PLC0415

        dispatch_session_start(
            session_id=sid,
            payload={
                "workspace_id": workspace_id,
                "parent_session_id": parent_session_id,
                "agent": dict(sess.agent),
                "mode": sess.mode,
            },
        )
        return sess

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid)

    def list(
        self,
        *,
        workspace_id: Optional[str] = None,
    ) -> list[Session]:
        """List sessions, newest-first. Optional workspace filter."""

        with self._lock:
            rows = list(self._sessions.values())
        if workspace_id:
            rows = [r for r in rows if r.workspace_id == workspace_id]
        # created_at is ISO-8601 UTC — lexicographic sort is
        # chronological here, so descending reverse-sort works.
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def delete(self, sid: str) -> bool:
        with self._lock:
            existing = self._sessions.get(sid)
            existed = existing is not None
            # #1115: a deleted session may still own LIVE remote SEP-2663 tasks. The
            # deletion is never blocked or delayed by them, but the rows are captured
            # here so they can be cancelled best-effort and migrated to the task
            # store's holding path instead of vanishing with the session.
            task_rows: dict[str, Any] = (
                dict((existing.metadata or {}).get("mcp_tasks") or {})
                if existing is not None
                else {}
            )
            self._sessions.pop(sid, None)
            if existed:
                self._flush()
        if existed:
            if task_rows:
                from clio_agent.gact.mcp_task_store import (  # noqa: PLC0415 - keep leaf
                    notify_session_deleted,
                )

                notify_session_deleted(sid, task_rows)
            # P2.3 SessionEnd lifecycle hook (observation): fires exactly once, only
            # when a session actually existed and was removed.
            from clio_agent.gact.hooks import dispatch_session_end  # noqa: PLC0415

            dispatch_session_end(session_id=sid)
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
        add_cost_usd: float = 0.0,
        mode: Optional[str] = None,
        edit_mode: Optional[str] = None,
        routing_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        model: Optional[dict[str, str]] = None,
        agent: Optional[dict[str, str]] = None,
        metadata_patch: Optional[dict[str, Any]] = None,
        archived: Optional[bool] = None,
    ) -> Optional[Session]:
        """Mutate a session in place.

        Any field left ``None`` is untouched. ``metadata_patch``
        merges into the existing metadata (shallow) so callers can
        stamp additional keys without clobbering the rest. The
        ``add_tokens_*`` / ``add_cost_usd`` params accumulate onto
        the session rollup — pass a turn's numbers after each
        forward() call.
        """

        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if title is not None:
                sess.title = title
            if status is not None:
                sess.status = status
            if message_count is not None:
                sess.message_count = message_count
            if add_tokens_input:
                sess.tokens_input += add_tokens_input
            if add_tokens_output:
                sess.tokens_output += add_tokens_output
            if add_cost_usd:
                sess.cost_usd += add_cost_usd
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
            }:
                sess.approval_mode = approval_mode
            if model is not None:
                sess.model = dict(model)
            if agent is not None:
                sess.agent = dict(agent)
            if metadata_patch is not None:
                sess.metadata.update(metadata_patch)
            if archived is not None:
                sess.archived = bool(archived)
            sess.updated_at = _utcnow_iso()
            self._flush()
            return sess

    # ---- introspection hooks (for /v1/memory/stats + tests) ----------

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)
