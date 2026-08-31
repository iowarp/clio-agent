"""Bounded transient-failure retry for clio-core native blob writes (#893).

Owner module (no-accretion: kept out of the ``storage.py`` god file). The
final live gate caught clio-core refusing writes transiently — ``PutBlob``
rc=13 with the ram tier well under its cap (an eviction/placement race), and
separately a container-restore gap right after a daemon restart ("Container
not found for pool_id 512.0"). A single-shot write turns those transients
into permanent transcript loss under the atoms regime's must-succeed ingest
(:class:`~clio_agent.gact.transcript_projection.TranscriptIngestError`), so
the store retries idempotently (same key re-put overwrites) a bounded number
of times, WARNS with a typed reason on every attempt, and then re-raises —
bounded and loud, never a silent fallback.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Typed reason for the per-attempt warning (queryable in logs/trace).
CLIO_CORE_PUT_RETRY = "clio_core_put_retry"
CLIO_CORE_PUT_PERMANENT_FAILURE = "clio_core_put_permanent_failure"

_ATTEMPTS = 3
_FIRST_DELAY_S = 0.2
_BACKOFF_FACTOR = 3.0


@dataclass(frozen=True, slots=True)
class ClioCorePutFailure:
    """Last permanent clio-core write failure observed by this process."""

    reason: str
    name: str
    payload_bytes: int
    error: str
    occurred_at: str

    def to_details(self) -> dict[str, str | int]:
        """Return JSON-safe health details without exposing payload content."""
        return asdict(self)


_failure_lock = threading.Lock()
_last_permanent_failure: ClioCorePutFailure | None = None


def _is_transient_put_failure(exc: RuntimeError) -> bool:
    """Return whether a native write refusal is safe to retry unchanged."""
    message = str(exc).lower()
    return "putblob operation failed (rc=13)" in message or (
        "container not found" in message and "pool_id 512.0" in message
    )


def _record_permanent_failure(*, name: str, payload: bytes, exc: RuntimeError) -> None:
    """Record and log one fail-stop write failure for health reporting."""
    global _last_permanent_failure
    record = ClioCorePutFailure(
        reason=CLIO_CORE_PUT_PERMANENT_FAILURE,
        name=name,
        payload_bytes=len(payload),
        error=str(exc),
        occurred_at=datetime.now(UTC).isoformat(),
    )
    with _failure_lock:
        _last_permanent_failure = record
    logger.error(
        "reason=%s name=%s payload_bytes=%d error=%s",
        record.reason,
        record.name,
        record.payload_bytes,
        record.error,
    )


def last_permanent_put_failure() -> ClioCorePutFailure | None:
    """Return the process-local permanent write failure, if one occurred."""
    with _failure_lock:
        return _last_permanent_failure


def _reset_permanent_put_failure_for_tests() -> None:
    """Reset the process-local failure latch for isolated tests."""
    global _last_permanent_failure
    with _failure_lock:
        _last_permanent_failure = None


def put_blob_with_retry(tag: Any, name: str, payload: bytes) -> None:
    """Write one blob, retrying only known transient native failures.

    Args:
        tag: A live CTE ``Tag`` handle.
        name: Blob name (re-putting the same name is an idempotent overwrite,
            which is what makes the retry safe).
        payload: The exact bytes to store.

    Raises:
        RuntimeError: A permanent failure immediately, or the final transient
            failure unmodified after :data:`_ATTEMPTS` tries.
    """
    delay = _FIRST_DELAY_S
    for attempt in range(_ATTEMPTS):
        try:
            tag.PutBlob(name, payload, 0)
            return
        except RuntimeError as exc:
            if not _is_transient_put_failure(exc):
                _record_permanent_failure(name=name, payload=payload, exc=exc)
                raise
            if attempt == _ATTEMPTS - 1:
                _record_permanent_failure(name=name, payload=payload, exc=exc)
                raise
            logger.warning(
                "reason=%s name=%s attempt=%d/%d error=%s",
                CLIO_CORE_PUT_RETRY,
                name,
                attempt + 1,
                _ATTEMPTS,
                exc,
            )
            time.sleep(delay)
            delay *= _BACKOFF_FACTOR
