"""Local model-limits database — the bottom of the limit-resolution cascade.

The handshake resolves a model's limits **provider-live -> models.dev -> this DB**.
The DB is a single JSON file that:

- **ships a seed in the repo** (so a fresh clone already knows common models, and the
  lab can share/grow one file), and
- is **written back on discovery**: whenever the handshake learns a model's context or
  output limit from a live provider, it is recorded here so a later offline run (or a
  teammate pulling the file) already has it.

When a freshly discovered live value disagrees with a stored one, the disagreement is
appended to a sibling ``*.mismatches.jsonl`` for review instead of being silently
overwritten-then-forgotten. Read-only installs (no write permission) degrade to
seed-only lookups — recording is best-effort and never raises.

This module replaces the older ``marketplace``/``static`` sources; their seeds live in
the shipped DB file (``data/model_limits.json``).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent.providers.handshake.sources._normalize import (
    iter_id_candidates,
    normalize_id,
)

_LOCK = threading.Lock()

#: Repo-shipped DB (read + written when writable; the lab shares/grows this file).
_DEFAULT_DB = Path(__file__).resolve().parent / "data" / "model_limits.json"


def db_path() -> Path:
    """The active DB file: ``CLIO_MODEL_DB`` env override, else the repo-shipped file."""
    override = os.environ.get("CLIO_MODEL_DB", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_DB


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _index(db: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalized-id + basename -> entry, mirroring the models.dev matching."""
    index: dict[str, dict[str, Any]] = {}
    for key, entry in db.items():
        if not isinstance(entry, dict):
            continue
        norm = normalize_id(key)
        if norm and norm not in index:
            index[norm] = entry
        if "/" in norm:
            base = norm.rsplit("/", 1)[1]
            if base and base not in index:
                index[base] = entry
    return index


def _lookup_field(model_id: str, field: str) -> int | None:
    db = _load(db_path())
    if not db:
        return None
    index = _index(db)
    for candidate in iter_id_candidates(model_id):
        entry = index.get(candidate)
        if entry is not None:
            value = entry.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


def lookup_context(model_id: str) -> int | None:
    """Return the stored context window for ``model_id``, or None."""
    return _lookup_field(model_id, "context")


def lookup_output(model_id: str) -> int | None:
    """Return the stored max output tokens for ``model_id``, or None."""
    return _lookup_field(model_id, "output")


def record(
    model_id: str,
    *,
    context: int | None = None,
    output: int | None = None,
    source: str = "live",
    provider: str = "",
) -> None:
    """Record discovered limits into the DB; log disagreements as mismatches.

    Best-effort: a read-only DB location (installed package) silently skips
    persistence — lookups still work from the shipped seed. Never raises.
    """
    if not (model_id or "").strip() or (not context and not output):
        return
    path = db_path()
    with _LOCK:
        db = _load(path)
        key = normalize_id(model_id)
        stored = db.get(key)
        entry: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        mismatches: list[dict[str, Any]] = []
        for field, value in (("context", context), ("output", output)):
            if not value or value <= 0:
                continue
            existing = entry.get(field)
            if (
                isinstance(existing, int)
                and not isinstance(existing, bool)
                and existing > 0
                and existing != value
            ):
                mismatches.append(
                    {
                        "model": model_id,
                        "field": field,
                        "stored": existing,
                        "discovered": value,
                        "source": source,
                        "provider": provider,
                    }
                )
            entry[field] = value
        entry["source"] = source
        if provider:
            entry["provider"] = provider
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        db[key] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(db, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            return  # read-only install: seed-only, recording skipped
        if mismatches:
            _log_mismatches(path, mismatches)


def _log_mismatches(path: Path, mismatches: list[dict[str, Any]]) -> None:
    try:
        out = path.with_suffix(".mismatches.jsonl")
        stamp = datetime.now(timezone.utc).isoformat()
        with out.open("a", encoding="utf-8") as fh:
            for m in mismatches:
                fh.write(json.dumps({**m, "at": stamp}) + "\n")
    except OSError:
        pass


def record_report(report: Any) -> None:
    """Record every live-discovered limit in a handshake report. Never raises."""
    for profile in getattr(report, "models", ()) or ():
        if getattr(profile, "context_source", "") != "live":
            continue
        ctx = getattr(profile, "context_window", None)
        out = getattr(profile, "output_limit", None)
        if ctx or out:
            record(
                profile.id,
                context=ctx,
                output=out,
                source="live",
                provider=getattr(report, "provider_kind", ""),
            )
