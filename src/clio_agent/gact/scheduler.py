"""Lightweight scheduler for recurring session turns (#21).

Each schedule pairs a cron expression with a question template +
the session it fires under. A background asyncio task ticks once
a minute, decides which schedules are due, and POSTs the resulting
question through the same _run_turn_in_background path the regular
HTTP handler uses.

No external dependencies — we parse a 5-field cron expression
ourselves (minute hour day-of-month month day-of-week, each
* | digit | comma-list | */N).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class Schedule:
    id: str
    session_id: str
    cron: str
    question: str
    enabled: bool = True
    created_at: str = ""
    last_fired_at: str = ""
    fire_count: int = 0

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _matches(field_value: int, expr: str) -> bool:
    """Check whether ``field_value`` matches a single cron field
    expression. Accepts ``*``, integer, comma list (1,2,3), or
    ``*/N`` step. No ranges (1-5) yet — keep it boring."""

    if expr == "*":
        return True
    if expr.startswith("*/"):
        try:
            step = int(expr[2:])
            return step > 0 and field_value % step == 0
        except ValueError:
            return False
    if "," in expr:
        try:
            return field_value in {int(p) for p in expr.split(",")}
        except ValueError:
            return False
    try:
        return field_value == int(expr)
    except ValueError:
        return False


def cron_matches(cron: str, when: datetime) -> bool:
    """Return True when ``when`` matches a 5-field cron expression
    in UTC. Day-of-week is 0=Sun .. 6=Sat (cron convention)."""

    parts = cron.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        _matches(when.minute, minute)
        and _matches(when.hour, hour)
        and _matches(when.day, dom)
        and _matches(when.month, month)
        and _matches((when.weekday() + 1) % 7, dow)
    )


class ScheduleStore:
    """Thread-safe schedule registry with optional JSON persistence."""

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._schedules: dict[str, Schedule] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row in data.get("schedules", []):
            try:
                self._schedules[row["id"]] = Schedule(**row)
            except Exception:
                continue

    def _flush(self) -> None:
        if self._path is None:
            return
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [s.to_wire() for s in self._schedules.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"schedules": rows}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def add(
        self, *, session_id: str, cron: str, question: str
    ) -> Schedule:
        sid = "sched_" + uuid.uuid4().hex[:12]
        sch = Schedule(
            id=sid,
            session_id=session_id,
            cron=cron,
            question=question,
            created_at=_utcnow_iso(),
        )
        with self._lock:
            self._schedules[sid] = sch
            self._flush()
        return sch

    def get(self, sid: str) -> Optional[Schedule]:
        with self._lock:
            return self._schedules.get(sid)

    def list(
        self, *, session_id: Optional[str] = None
    ) -> list[Schedule]:
        with self._lock:
            rows = list(self._schedules.values())
        if session_id is not None:
            rows = [r for r in rows if r.session_id == session_id]
        return sorted(rows, key=lambda s: s.created_at)

    def delete(self, sid: str) -> bool:
        with self._lock:
            existed = sid in self._schedules
            self._schedules.pop(sid, None)
            self._flush()
        return existed

    def mark_fired(self, sid: str) -> None:
        with self._lock:
            sch = self._schedules.get(sid)
            if sch is None:
                return
            sch.last_fired_at = _utcnow_iso()
            sch.fire_count += 1
            self._flush()

    def due_now(self, when: datetime) -> Iterable[Schedule]:
        with self._lock:
            rows = list(self._schedules.values())
        # Truncate to minute precision so multiple ticks within
        # the same minute don't fire twice.
        when_minute = when.replace(second=0, microsecond=0).isoformat()
        for sch in rows:
            if not sch.enabled:
                continue
            if sch.last_fired_at and sch.last_fired_at.startswith(
                when_minute
            ):
                continue
            if cron_matches(sch.cron, when):
                yield sch
