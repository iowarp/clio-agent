"""Compaction's audit records, also traced: a skipped or failed auto compaction is never
visible only in the optional stream-audit log.

``stream_audit`` writes only when ``CLIO_STREAM_AUDIT_LOG`` is set, and every reason auto
compaction gave for not running went there alone, so a run whose context crossed the
threshold without compacting left no explanation (no-silent-fallback). The compaction
module imports :func:`stream_audit` from here: it writes the audit record as before and
traces the auto-compaction stages with their fields.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit as _audit

#: The auto-compaction stages that are also traced (the compaction module's names).
TRACED_STAGES = frozenset({"compaction.auto_failed", "compaction.auto_skipped"})


def stream_audit(stage: str, /, **fields: Any) -> None:
    """Write the audit record; trace it too when it is an auto-compaction stage."""
    _audit(stage, **fields)
    if stage in TRACED_STAGES:
        trace.event("COMPACT", "%s %s", stage, " ".join(f"{k}={v}" for k, v in fields.items()))
