"""Durable pending-steer, queued-message, and idempotency state.

The transcript, a pending steer, and a queued future message are deliberately
different entities. This store persists the two pre-transcript lifecycle planes
and the acceptance idempotency index without making the React client authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from clio_agent.gact.types import Message, MessageBehavior, ModelRef, Part, PostMessageResponse

logger = logging.getLogger(__name__)
T = TypeVar("T")

_SETTLED_STEER_STATES = frozenset({"consumed", "cancelled"})


@dataclass(frozen=True)
class IntentRetention:
    """Per-session bounds on the three durable intent planes.

    Every plane grew without limit: a settled steer and an acceptance record were
    written on every mutation and never removed, so a long-lived session's store
    grew forever and each flush rewrote all of it.

    The two bounds behave DIFFERENTLY on purpose. Settled steers and acceptances
    are HISTORY -- the oldest are evicted with a typed reason. Queued messages are
    the user's un-sent future intent, so the cap REFUSES a new one
    (``QueueCapacityError``) rather than silently dropping something they wrote.
    """

    max_queued_per_session: int
    max_settled_steers_per_session: int
    max_acceptances_per_session: int

    @classmethod
    def from_conf(cls) -> "IntentRetention":
        """Resolve the bounds through the config layer (file -> env -> default)."""

        from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

        return cls(
            max_queued_per_session=conf.resolve(
                "gact.message_intents.max_queued_per_session",
                env="CLIO_GACT_MAX_QUEUED_MESSAGES_PER_SESSION",
                default=100,
                cast=conf.as_int,
            ),
            max_settled_steers_per_session=conf.resolve(
                "gact.message_intents.max_settled_steers_per_session",
                env="CLIO_GACT_MAX_SETTLED_STEERS_PER_SESSION",
                default=100,
                cast=conf.as_int,
            ),
            max_acceptances_per_session=conf.resolve(
                "gact.message_intents.max_acceptances_per_session",
                env="CLIO_GACT_MAX_ACCEPTANCES_PER_SESSION",
                default=200,
                cast=conf.as_int,
            ),
        )


def stage_intent_user_message(
    app: Any,
    session_id: str,
    message: Message,
    *,
    replace_existing: bool,
) -> None:
    """Append a new user message or replace its persisted pending-steer identity."""

    from clio_agent.gact.app import (  # noqa: PLC0415
        _append_session_message,
        _replace_session_messages,
    )

    if not replace_existing:
        _append_session_message(app, session_id, message)
        return
    messages = list(app.state.messages.get(session_id, []))
    if not any(current.id == message.id for current in messages):
        raise ValueError(f"pending user message not found: {message.id}")
    _replace_session_messages(
        app,
        session_id,
        [message if current.id == message.id else current for current in messages],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingSteer(BaseModel):
    """A persisted user message accepted for the next safe model boundary."""

    message_id: str
    session_id: str
    parts: list[Part] = Field(default_factory=list)
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    accepted_at: str
    behavior: MessageBehavior = Field(default_factory=MessageBehavior)
    model: ModelRef = Field(default_factory=ModelRef)
    state: Literal["pending", "claimed", "consumed", "cancelled"] = "pending"
    claimed_at: str = ""
    consumed_at: str = ""
    cancelled_at: str = ""


class QueuedMessage(BaseModel):
    """A durable, editable future message that is not transcript state yet."""

    id: str
    session_id: str
    revision: int = 1
    position: int = 0
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    client_message_id: str = ""
    idempotency_key: str = ""
    behavior: MessageBehavior = Field(default_factory=MessageBehavior)
    model: ModelRef = Field(default_factory=ModelRef)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class RevisionConflictError(ValueError):
    """Raised when a queued-message mutation uses a stale revision."""

    def __init__(self, current: QueuedMessage) -> None:
        super().__init__("queued message revision conflict")
        self.current = current


class IntentStoreReadError(RuntimeError):
    """Raised when persisted message-intent state cannot be read safely."""


class DuplicateIntentError(ValueError):
    """Raised when a client-provided identity already names another intent."""


class QueueCapacityError(ValueError):
    """Raised when a session's queue is already at its configured cap."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"queued message limit reached: {limit}")
        self.limit = limit


class MessageIntentStore:
    """Thread-safe JSON persistence for message intent outside the transcript."""

    def __init__(self, path: Path, *, retention: IntentRetention | None = None) -> None:
        self._path = path
        self._retention = retention or IntentRetention.from_conf()
        self._lock = threading.RLock()
        self._pending: dict[str, PendingSteer] = {}
        self._queued: dict[str, QueuedMessage] = {}
        self._acceptances: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def retention(self) -> IntentRetention:
        """The per-session bounds this store enforces."""

        return self._retention

    def acceptance(self, session_id: str, key: str) -> PostMessageResponse | None:
        """Return a prior acceptance for this session/key, if one exists."""

        if not key:
            return None
        with self._lock:
            payload = self._acceptances.get(f"{session_id}:{key}")
            return PostMessageResponse(**payload) if payload else None

    def record_acceptance(self, session_id: str, key: str, response: PostMessageResponse) -> None:
        """Persist an acceptance response for idempotent replay."""

        if not key:
            return
        with self._lock:
            index_key = f"{session_id}:{key}"
            existing = self._acceptances.get(index_key)
            serialized = response.model_dump()
            if existing is not None and existing != serialized:
                raise DuplicateIntentError(
                    "idempotency key already names a different accepted message"
                )
            self._acceptances[index_key] = serialized
            self._prune_acceptances_locked(session_id)
            self._flush_locked()

    def add_pending(self, pending: PendingSteer) -> None:
        """Persist a pending steer before its HTTP acceptance returns."""

        with self._lock:
            existing = self._pending.get(pending.message_id)
            if existing is not None and existing != pending:
                raise DuplicateIntentError("message id already names a different pending steer")
            self._pending[pending.message_id] = pending.model_copy(deep=True)
            self._flush_locked()

    def accept_pending(
        self,
        pending: PendingSteer,
        key: str,
        response: PostMessageResponse,
    ) -> PostMessageResponse | None:
        """Atomically persist a pending steer and its idempotent acceptance."""

        with self._lock:
            index_key = f"{pending.session_id}:{key}" if key else ""
            if index_key:
                prior = self._acceptances.get(index_key)
                if prior is not None:
                    return PostMessageResponse(**prior)
            existing = self._pending.get(pending.message_id)
            if existing is not None and existing != pending:
                raise DuplicateIntentError("message id already names a different pending steer")
            self._pending[pending.message_id] = pending.model_copy(deep=True)
            if index_key:
                self._acceptances[index_key] = response.model_dump()
                self._prune_acceptances_locked(pending.session_id)
            self._flush_locked()
            return None

    def discard_pending(
        self, session_id: str, message_id: str, *, acceptance_key: str = ""
    ) -> None:
        """Remove an unacknowledged pending steer after acceptance rollback."""

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id:
                return
            self._pending.pop(message_id)
            if acceptance_key:
                self._acceptances.pop(f"{session_id}:{acceptance_key}", None)
            self._flush_locked()

    def list_pending(self, session_id: str) -> list[PendingSteer]:
        """List accepted, not-yet-consumed steers in acceptance order."""

        with self._lock:
            rows = [
                row.model_copy(deep=True)
                for row in self._pending.values()
                if row.session_id == session_id and row.state in {"pending", "claimed"}
            ]
        return sorted(rows, key=lambda row: row.accepted_at)

    def list_all_pending(self) -> list[PendingSteer]:
        """List every accepted, not-yet-consumed steer across sessions."""

        with self._lock:
            rows = [
                row.model_copy(deep=True)
                for row in self._pending.values()
                if row.state in {"pending", "claimed"}
            ]
        return sorted(rows, key=lambda row: (row.session_id, row.accepted_at, row.message_id))

    def get_pending(self, session_id: str, message_id: str) -> PendingSteer | None:
        """Read one pending-steer lifecycle row regardless of terminal state."""

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            return row.model_copy(deep=True)

    def claim_pending(self, session_id: str, message_id: str) -> PendingSteer | None:
        """Atomically claim one steer for delivery at a safe boundary."""

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id or row.state != "pending":
                return None
            row.state = "claimed"
            row.claimed_at = _now_iso()
            self._flush_locked()
            return row.model_copy(deep=True)

    def release_claim(self, session_id: str, message_id: str) -> PendingSteer | None:
        """Return a claimed-but-unsurfaced steer to ``pending``.

        A claim is a delivery reservation, not a settlement. When the consumer
        that claimed a steer cannot surface it (nothing describable to compose,
        a settle failure) the reservation MUST be given back — otherwise the row
        sits ``claimed`` forever: never delivered, never re-driven, and (before
        :meth:`cancel_pending` learned the ``claimed`` state) uncancellable.
        """

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id or row.state != "claimed":
                return None
            row.state = "pending"
            row.claimed_at = ""
            self._flush_locked()
            return row.model_copy(deep=True)

    def cancel_pending(self, session_id: str, message_id: str) -> PendingSteer | None:
        """Mark a not-yet-consumed steer cancelled.

        Cancels from ``pending`` AND from ``claimed``: a claim can outlive the
        consumer that took it (a crashed drain, a turn torn down between claim
        and settle), and a steer nobody can cancel is a permanent lie in the
        client's composer. Consumption is the one terminal a cancel cannot
        reach — by then the model has already been steered.
        """

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            if row.state not in {"pending", "claimed"}:
                return None
            row.state = "cancelled"
            row.cancelled_at = _now_iso()
            settled = row.model_copy(deep=True)
            self._prune_settled_steers_locked(session_id)
            self._flush_locked()
            return settled

    def mark_consumed(self, session_id: str, message_id: str) -> PendingSteer | None:
        """Settle a claimed steer after its transcript event is published."""

        with self._lock:
            row = self._pending.get(message_id)
            if row is None or row.session_id != session_id or row.state != "claimed":
                return None
            row.state = "consumed"
            row.consumed_at = _now_iso()
            settled = row.model_copy(deep=True)
            self._prune_settled_steers_locked(session_id)
            self._flush_locked()
            return settled

    def list_queued(self, session_id: str) -> list[QueuedMessage]:
        """Return authoritative server order for a session's queued messages."""

        with self._lock:
            rows = [
                row.model_copy(deep=True)
                for row in self._queued.values()
                if row.session_id == session_id
            ]
        return sorted(rows, key=lambda row: (row.position, row.created_at, row.id))

    def create_queued(self, row: QueuedMessage) -> QueuedMessage:
        """Append a queued message in authoritative server order."""

        with self._lock:
            existing = self._queued.get(row.id)
            if existing is not None:
                if existing.session_id == row.session_id:
                    return existing.model_copy(deep=True)
                raise DuplicateIntentError("queued message id already belongs to another session")
            positions = [
                queued.position
                for queued in self._queued.values()
                if queued.session_id == row.session_id
            ]
            limit = self._retention.max_queued_per_session
            if len(positions) >= limit:
                # A REFUSAL, not an eviction: a queued message is the user's
                # un-sent future intent and must never disappear behind their
                # back. The typed error names the limit so a client can say why.
                raise QueueCapacityError(limit)
            row.position = max(positions, default=-1) + 1
            self._queued[row.id] = row.model_copy(deep=True)
            self._flush_locked()
            return row.model_copy(deep=True)

    def find_queued_by_idempotency(self, session_id: str, key: str) -> QueuedMessage | None:
        """Return the queued message already accepted for a client key."""

        if not key:
            return None
        with self._lock:
            for row in self._queued.values():
                if row.session_id == session_id and row.idempotency_key == key:
                    return row.model_copy(deep=True)
        return None

    def update_queued(
        self,
        session_id: str,
        message_id: str,
        revision: int,
        *,
        parts: list[Part] | None = None,
        metadata: dict[str, Any] | None = None,
        behavior: MessageBehavior | None = None,
        model: ModelRef | None = None,
    ) -> QueuedMessage | None:
        """Apply one revision-checked queued-message edit."""

        with self._lock:
            row = self._queued.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            if row.revision != revision:
                raise RevisionConflictError(row.model_copy(deep=True))
            if parts is not None:
                row.parts = list(parts)
            if metadata is not None:
                row.metadata = dict(metadata)
            if behavior is not None:
                row.behavior = behavior
            if model is not None:
                row.model = model
            row.revision += 1
            row.updated_at = _now_iso()
            self._flush_locked()
            return row.model_copy(deep=True)

    def delete_queued(
        self, session_id: str, message_id: str, revision: int
    ) -> QueuedMessage | None:
        """Delete one queued message if its revision still matches."""

        with self._lock:
            row = self._queued.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            if row.revision != revision:
                raise RevisionConflictError(row.model_copy(deep=True))
            deleted = self._queued.pop(message_id)
            self._normalize_positions_locked(session_id)
            self._flush_locked()
            return deleted.model_copy(deep=True)

    def reorder(
        self,
        session_id: str,
        ordered_ids: list[str],
        expected_revisions: dict[str, int],
    ) -> list[QueuedMessage]:
        """Replace queue order; supplied IDs must exactly match server state."""

        with self._lock:
            current = {row.id: row for row in self._queued.values() if row.session_id == session_id}
            if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != set(current):
                raise ValueError("queued message reorder set does not match server state")
            for message_id, row in current.items():
                if expected_revisions.get(message_id) != row.revision:
                    raise RevisionConflictError(row.model_copy(deep=True))
            stamp = _now_iso()
            for position, message_id in enumerate(ordered_ids):
                row = current[message_id]
                if row.position != position:
                    row.position = position
                    row.revision += 1
                    row.updated_at = stamp
            self._flush_locked()
            return [current[message_id].model_copy(deep=True) for message_id in ordered_ids]

    def get_queued(self, session_id: str, message_id: str) -> QueuedMessage | None:
        """Read one queued message without claiming or deleting it."""

        with self._lock:
            row = self._queued.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            return row.model_copy(deep=True)

    def promote_queued(
        self,
        session_id: str,
        message_id: str,
        revision: int,
        promote: Callable[[QueuedMessage], T],
    ) -> tuple[QueuedMessage, T] | None:
        """Accept and remove one queued row as one revision-checked transaction.

        RESERVE-then-accept, not accept-under-lock. The row is removed from the
        queue under the lock, which is what actually makes the transaction
        exclusive — edit, delete, reorder, a manual promotion and the idle
        auto-dispatch all miss a row that is no longer in ``_queued``. The
        callback then runs OUTSIDE the lock: it stages a whole turn (transcript
        write, provider validation, background task), and holding the store's
        RLock across that blocked every other queue reader for its duration.

        If acceptance raises, the reservation is rolled back — the row is durable
        and editable again at its ORIGINAL revision, so a client's revision guard
        still matches. Acceptance idempotency makes a process interruption after
        acceptance but before removal safe to replay on the next attempt.
        """

        with self._lock:
            row = self._queued.get(message_id)
            if row is None or row.session_id != session_id:
                return None
            if row.revision != revision:
                raise RevisionConflictError(row.model_copy(deep=True))
            reserved = self._queued.pop(message_id)
            self._normalize_positions_locked(session_id)
            self._flush_locked()
        try:
            result = promote(reserved.model_copy(deep=True))
        except BaseException:
            with self._lock:
                self._queued[reserved.id] = reserved
                self._normalize_positions_locked(session_id)
                self._flush_locked()
            raise
        return reserved.model_copy(deep=True), result

    def delete_session(self, session_id: str) -> None:
        """Remove every pending, queued, and idempotency row owned by a session."""

        with self._lock:
            pending_ids = [
                message_id
                for message_id, row in self._pending.items()
                if row.session_id == session_id
            ]
            queued_ids = [
                message_id
                for message_id, row in self._queued.items()
                if row.session_id == session_id
            ]
            acceptance_keys = [key for key in self._acceptances if key.startswith(f"{session_id}:")]
            if not pending_ids and not queued_ids and not acceptance_keys:
                return
            for message_id in pending_ids:
                self._pending.pop(message_id, None)
            for message_id in queued_ids:
                self._queued.pop(message_id, None)
            for key in acceptance_keys:
                self._acceptances.pop(key, None)
            self._flush_locked()

    def _prune_acceptances_locked(self, session_id: str) -> None:
        """Evict the OLDEST acceptances past the per-session cap, with a reason.

        An acceptance record only serves idempotent replay of a POST the client
        may retry; once it is far enough in the past no client will ask again.
        Eviction is therefore safe, and never silent: an evicted key that IS
        replayed re-accepts, which the log line below makes diagnosable.
        """

        limit = self._retention.max_acceptances_per_session
        prefix = f"{session_id}:"
        keys = [key for key in self._acceptances if key.startswith(prefix)]
        if len(keys) <= limit:
            return
        ordered = sorted(keys, key=lambda key: str(self._acceptances[key].get("accepted_at") or ""))
        for key in ordered[: len(keys) - limit]:
            self._acceptances.pop(key, None)
        logger.info(
            "message-intent acceptances pruned session=%s reason=acceptance_retention "
            "limit=%d evicted=%d",
            session_id,
            limit,
            len(keys) - limit,
        )

    def _prune_settled_steers_locked(self, session_id: str) -> None:
        """Evict the OLDEST settled steers past the cap. ACTIVE rows never go.

        ``consumed`` and ``cancelled`` rows are history a client reconciles
        against; ``pending`` and ``claimed`` rows are undelivered user intent and
        are exempt from every bound (dropping one would lose a message the server
        already accepted with a 202).
        """

        limit = self._retention.max_settled_steers_per_session
        settled = [
            row
            for row in self._pending.values()
            if row.session_id == session_id and row.state in _SETTLED_STEER_STATES
        ]
        if len(settled) <= limit:
            return
        settled.sort(key=lambda row: (row.consumed_at or row.cancelled_at or row.accepted_at))
        for row in settled[: len(settled) - limit]:
            self._pending.pop(row.message_id, None)
        logger.info(
            "message-intent settled steers pruned session=%s reason=steer_retention "
            "limit=%d evicted=%d",
            session_id,
            limit,
            len(settled) - limit,
        )

    def _normalize_positions_locked(self, session_id: str) -> None:
        rows = sorted(
            (row for row in self._queued.values() if row.session_id == session_id),
            key=lambda row: (row.position, row.created_at, row.id),
        )
        for position, row in enumerate(rows):
            row.position = position

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("unable to read persisted message intent state", exc_info=exc)
            raise IntentStoreReadError(
                f"unable to read message intent state at {self._path}"
            ) from exc
        if not isinstance(payload, dict):
            raise IntentStoreReadError(f"message intent state at {self._path} is not an object")
        raw_pending = payload.get("pending", [])
        raw_queued = payload.get("queued", [])
        raw_acceptances = payload.get("acceptances", {})
        if not isinstance(raw_pending, list):
            raise IntentStoreReadError("message intent pending state is not a list")
        if not isinstance(raw_queued, list):
            raise IntentStoreReadError("message intent queue state is not a list")
        if not isinstance(raw_acceptances, dict):
            raise IntentStoreReadError("message intent acceptance state is not an object")

        pending: dict[str, PendingSteer] = {}
        queued: dict[str, QueuedMessage] = {}
        acceptances: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_pending):
            try:
                pending_row = PendingSteer(**raw)
                if pending_row.state == "claimed":
                    pending_row.state = "pending"
                    pending_row.claimed_at = ""
                if pending_row.message_id in pending:
                    raise ValueError("duplicate pending message id")
                pending[pending_row.message_id] = pending_row
            except (TypeError, ValueError) as exc:
                raise IntentStoreReadError(
                    f"invalid pending message intent at index {index}"
                ) from exc
        for index, raw in enumerate(raw_queued):
            try:
                queued_row = QueuedMessage(**raw)
                if queued_row.id in queued:
                    raise ValueError("duplicate queued message id")
                queued[queued_row.id] = queued_row
            except (TypeError, ValueError) as exc:
                raise IntentStoreReadError(
                    f"invalid queued message intent at index {index}"
                ) from exc
        for key, raw in raw_acceptances.items():
            try:
                response = PostMessageResponse(**raw)
            except (TypeError, ValueError) as exc:
                raise IntentStoreReadError(f"invalid message acceptance for key {key!r}") from exc
            acceptances[str(key)] = response.model_dump()

        self._pending = pending
        self._queued = queued
        self._acceptances = acceptances

    def _flush_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pending": [row.model_dump() for row in self._pending.values()],
            "queued": [row.model_dump() for row in self._queued.values()],
            "acceptances": self._acceptances,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        # Compact, not pretty: this rewrites the WHOLE store on every mutation
        # (one file, no per-session shard), so indent=2 + sort_keys multiplied the
        # bytes written on the acceptance hot path for no operator benefit. The
        # write stays SYNCHRONOUS on purpose -- POST /messages promises a durable
        # steer before it returns its 202, so a write-behind queue would turn that
        # contract into a lie on any crash. The retention bounds above are what
        # keep the write cheap.
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self._path)


__all__ = [
    "DuplicateIntentError",
    "IntentRetention",
    "IntentStoreReadError",
    "MessageIntentStore",
    "PendingSteer",
    "QueueCapacityError",
    "QueuedMessage",
    "RevisionConflictError",
]
