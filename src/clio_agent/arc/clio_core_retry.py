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

Two rules govern this module, both learned by breaking them:

* **No message-text classification.** A native ``PutBlob`` refusal arrives as
  a bare :class:`RuntimeError` whose text carries no structured code, so clio
  cannot tell transient from permanent from the outside. Splitting on captured
  incident strings (``rc=13``, ``pool_id 512.0``) silently stripped the retry
  from every deployment whose text differed. Every native refusal therefore
  takes the SAME bounded retry; only the OUTCOME is classified.
* **Write health is live state, not a latch.** An exhausted retry means the
  write was lost and clio-core is refusing writes RIGHT NOW
  (:func:`last_lost_put_write` -> the required ``clio_core_write`` doctor row
  -> ``/v1/health`` 503). The very next successful write clears it with its own
  typed reason, so a recovered backend stops reporting hard-down without a
  restart.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Typed reasons for the write lifecycle (queryable in logs/trace).
CLIO_CORE_PUT_RETRY = "clio_core_put_retry"
CLIO_CORE_PUT_WRITE_LOST = "clio_core_put_write_lost"
CLIO_CORE_PUT_WRITE_RECOVERED = "clio_core_put_write_recovered"


def write_retry_attempts() -> int:
    """Total tries (not extra retries) for one clio-core native blob write.

    Config: ``arc.clio_core_write_retry.attempts`` /
    ``CLIO_ARC_CLIO_CORE_WRITE_RETRY_ATTEMPTS`` (default 3). Raise it for a
    backend observed to refuse writes in longer bursts; a value of 1 disables
    the retry and turns every transient refusal into a lost write.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "arc.clio_core_write_retry.attempts",
        env="CLIO_ARC_CLIO_CORE_WRITE_RETRY_ATTEMPTS",
        default=3,
        cast=conf.as_int,
    )


def write_retry_first_delay_s() -> float:
    """Seconds waited before the FIRST clio-core write retry.

    Config: ``arc.clio_core_write_retry.first_delay_s`` /
    ``CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S`` (default 0.2). Lengthen it
    when the observed refusals are eviction/placement races that need longer to
    clear.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "arc.clio_core_write_retry.first_delay_s",
        env="CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S",
        default=0.2,
        cast=conf.as_float,
    )


def write_retry_backoff_factor() -> float:
    """Multiplier applied to the clio-core write-retry delay after each attempt.

    Config: ``arc.clio_core_write_retry.backoff_factor`` /
    ``CLIO_ARC_CLIO_CORE_WRITE_RETRY_BACKOFF_FACTOR`` (default 3.0,
    dimensionless). 1.0 makes the ladder flat; raise it to back off harder from
    a struggling daemon.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "arc.clio_core_write_retry.backoff_factor",
        env="CLIO_ARC_CLIO_CORE_WRITE_RETRY_BACKOFF_FACTOR",
        default=3.0,
        cast=conf.as_float,
    )


@dataclass(frozen=True, slots=True)
class ClioCorePutFailure:
    """A clio-core write this process lost, with no success observed since."""

    reason: str
    name: str
    payload_bytes: int
    error: str
    occurred_at: str

    def to_details(self) -> dict[str, str | int]:
        """Return JSON-safe health details without exposing payload content."""
        return asdict(self)


_failure_lock = threading.Lock()
_lost_write: ClioCorePutFailure | None = None


def _record_lost_write(*, name: str, payload: bytes, exc: RuntimeError) -> None:
    """Record and log one lost clio-core write for health reporting.

    Args:
        name: Blob name whose write was lost.
        payload: The bytes that never landed (only their length is retained).
        exc: The final native refusal, verbatim.
    """
    global _lost_write
    record = ClioCorePutFailure(
        reason=CLIO_CORE_PUT_WRITE_LOST,
        name=name,
        payload_bytes=len(payload),
        error=str(exc),
        occurred_at=datetime.now(UTC).isoformat(),
    )
    with _failure_lock:
        _lost_write = record
    logger.error(
        "reason=%s name=%s payload_bytes=%d error=%s",
        record.reason,
        record.name,
        record.payload_bytes,
        record.error,
    )


def _record_put_success() -> None:
    """Clear write health after a successful put, loudly and only if it was set."""
    global _lost_write
    with _failure_lock:
        recovered, _lost_write = _lost_write, None
    if recovered is None:
        return
    logger.warning(
        "reason=%s recovered_after=%s lost_name=%s lost_payload_bytes=%d lost_error=%s",
        CLIO_CORE_PUT_WRITE_RECOVERED,
        recovered.occurred_at,
        recovered.name,
        recovered.payload_bytes,
        recovered.error,
    )


def last_lost_put_write() -> ClioCorePutFailure | None:
    """Return the lost write this process has not yet recovered from, if any."""
    with _failure_lock:
        return _lost_write


def _reset_put_write_health_for_tests() -> None:
    """Reset the process-local write-health state for isolated tests."""
    global _lost_write
    with _failure_lock:
        _lost_write = None


def put_blob_with_retry(tag: Any, name: str, payload: bytes) -> None:
    """Write one blob, retrying EVERY native refusal a bounded number of times.

    A native refusal carries no structured code, so its transience is not
    knowable here; the idempotent re-put makes retrying every one of them safe,
    and only the outcome is classified (lost write vs. recovered write health).

    Args:
        tag: A live CTE ``Tag`` handle.
        name: Blob name (re-putting the same name is an idempotent overwrite,
            which is what makes the retry safe).
        payload: The exact bytes to store.

    Raises:
        RuntimeError: The final native refusal unmodified after
            :func:`write_retry_attempts` tries, having recorded the lost write.
    """
    # Resolved once per write, never inside the retry loop.
    attempts = max(1, write_retry_attempts())
    backoff_factor = write_retry_backoff_factor()
    delay = write_retry_first_delay_s()
    for attempt in range(attempts):
        try:
            tag.PutBlob(name, payload, 0)
        except RuntimeError as exc:
            if attempt == attempts - 1:
                _record_lost_write(name=name, payload=payload, exc=exc)
                raise
            logger.warning(
                "reason=%s name=%s attempt=%d/%d error=%s",
                CLIO_CORE_PUT_RETRY,
                name,
                attempt + 1,
                attempts,
                exc,
            )
            time.sleep(delay)
            delay *= backoff_factor
        else:
            _record_put_success()
            return
