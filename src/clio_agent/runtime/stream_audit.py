"""Timestamped stream-audit JSONL for provider-to-SSE verification."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def stream_audit_enabled() -> bool:
    """Return whether stream audit JSONL logging is enabled."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return bool(
        conf.resolve(
            "debug.stream_audit_log", env="CLIO_STREAM_AUDIT_LOG", default="", cast=conf.as_str
        ).strip()
    )


def stream_audit(stage: str, **fields: Any) -> None:
    """Append one timestamped stream-audit record if configured.

    The audit log is intentionally separate from the normal trace logger: it is
    low-level evidence used to compare raw provider chunk generation, bridge
    scheduling, normalized transcript events, and raw SSE receive times.
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    raw_path = conf.resolve(
        "debug.stream_audit_log", env="CLIO_STREAM_AUDIT_LOG", default="", cast=conf.as_str
    ).strip()
    if not raw_path:
        return
    now = time.time()
    row = {
        "ts": now,
        "iso": _utc_iso(now),
        "stage": stage,
        **fields,
    }
    try:
        path = Path(raw_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, sort_keys=True, default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
    except Exception:  # noqa: BLE001 - audit-log write best-effort; skipped on failure
        return
