"""Local model-limits database — the bottom of the limit-resolution cascade.

The handshake resolves a model's limits **provider-live -> models.dev -> this DB**.
The DB is split into two layers:

- a **read-only seed shipped with the package** (``data/model_limits.json``) so a
  fresh install already knows common models — it is **never written at runtime**
  (writing it kept the git tree permanently dirty and silently no-oped on read-only
  pip installs, #763), and
- a **user DB** under :func:`clio_agent.paths.user_data_dir` (or the ``CLIO_MODEL_DB``
  override) that is **written back on discovery**: whenever the handshake learns a
  model's context or output limit from a live provider, it is recorded there so a
  later offline run already has it. Lookups merge the seed beneath the user DB —
  user entries win.

When a freshly discovered live value disagrees with a stored one, the disagreement is
appended to a sibling ``*.mismatches.jsonl`` for review instead of being silently
overwritten-then-forgotten. An unwritable DB location degrades to lookup-only —
recording is best-effort, logs a structured warning, and never raises.

This module replaces the older ``marketplace``/``static`` sources; their seeds live in
the shipped DB file (``data/model_limits.json``).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.providers.handshake.sources._normalize import (
    iter_id_candidates,
    normalize_id,
)

_LOGGER = logging.getLogger(__name__)

_LOCK = threading.Lock()

#: Packaged read-only seed — never written at runtime (#763).
_SEED_DB = Path(__file__).resolve().parent / "data" / "model_limits.json"


def db_path() -> Path:
    """The writable DB file: ``CLIO_MODEL_DB`` env override, else the user data dir."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    override = conf.resolve(
        "paths.model_db", env="CLIO_MODEL_DB", default="", cast=conf.as_str
    ).strip()
    if override:
        return Path(override).expanduser()
    return paths.user_data_dir() / "model_limits.json"


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_merged(path: Path) -> dict[str, dict[str, Any]]:
    """The user DB with the packaged seed merged beneath it (user entries win)."""
    merged = dict(_load(path))
    if path != _SEED_DB:  # guard: CLIO_MODEL_DB pointed at the seed itself
        for key, entry in _load(_SEED_DB).items():
            merged.setdefault(key, entry)
    return merged


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
    db = _load_merged(db_path())
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
    """Record discovered limits into the user DB; log disagreements as mismatches.

    Writes only the user DB (or the ``CLIO_MODEL_DB`` override) — never the packaged
    seed. Best-effort: an unwritable DB location skips persistence with a structured
    warning — lookups still work from the shipped seed. Never raises.
    """
    if not (model_id or "").strip() or (not context and not output):
        return
    path = db_path()
    with _LOCK:
        db = _load(path)
        key = normalize_id(model_id)
        # Compare against the merged view so a live value that disagrees with a
        # seed entry is still surfaced as a mismatch.
        stored = _load_merged(path).get(key)
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
        except OSError as exc:
            # Degraded path: lookups keep working (seed + whatever was readable);
            # only persistence of the newly discovered limit is lost.
            _LOGGER.warning(
                "model_db_record_skipped reason=db_unwritable path=%s model=%s error=%s",
                path,
                model_id,
                exc,
            )
            return
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
