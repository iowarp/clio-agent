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
import time
from typing import Any

logger = logging.getLogger(__name__)

# Typed reason for the per-attempt warning (queryable in logs/trace).
CLIO_CORE_PUT_RETRY = "clio_core_put_retry"

_ATTEMPTS = 3
_FIRST_DELAY_S = 0.2
_BACKOFF_FACTOR = 3.0


def put_blob_with_retry(tag: Any, name: str, payload: bytes) -> None:
    """``tag.PutBlob(name, payload, 0)`` with bounded retry on RuntimeError.

    Args:
        tag: A live CTE ``Tag`` handle.
        name: Blob name (re-putting the same name is an idempotent overwrite,
            which is what makes the retry safe).
        payload: The exact bytes to store.

    Raises:
        RuntimeError: The final attempt's failure, unmodified, after
            :data:`_ATTEMPTS` tries.
    """
    delay = _FIRST_DELAY_S
    for attempt in range(_ATTEMPTS):
        try:
            tag.PutBlob(name, payload, 0)
            return
        except RuntimeError as exc:
            if attempt == _ATTEMPTS - 1:
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
