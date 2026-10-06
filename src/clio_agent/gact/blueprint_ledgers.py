"""Atomic persistence for marketplace lifecycle decisions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Replace a JSON ledger only after the complete replacement is flushed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".ledger-", delete=False
        ) as handle:
            pending = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def forgotten_sources(path: Path) -> set[str]:
    """Read explicit source removals; an unreadable ledger must not resurrect them."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if not isinstance(payload, dict) or not isinstance(payload.get("forgotten"), list):
        raise ValueError("invalid marketplace removal ledger")
    return {str(value) for value in payload["forgotten"]}


def set_source_forgotten(path: Path, source_id: str, forgotten: bool) -> None:
    """Record removal or an explicit re-registration under the caller's source lock."""
    removed = forgotten_sources(path)
    if forgotten:
        removed.add(source_id)
    else:
        removed.discard(source_id)
    write_json_atomic(path, {"forgotten": sorted(removed)})
