"""Owner module for the persisted agent-blueprint SOURCE registry.

The source registry is the per-user ledger of marketplace/source registrations
(``agent-blueprint-sources.json``). Carved out of ``routes/blueprints.py``
(#775 no-accretion — the routes file is ratchet-baselined) when the registry
gained self-healing:

* **Dead-fixture pruning** — ~100 rows pointing at long-deleted pytest temp
  dirs accumulated because the tests' XDG-based isolation is a no-op on
  Windows (platformdirs ignores ``XDG_CONFIG_HOME`` there), so every test run
  registered its tmpdir fixture into the REAL registry. Pruning is
  deliberately narrow: only local paths that are gone AND look like temp/test
  residue are dropped — a source on an unmounted network drive or an SCP-style
  remote (``host:path``) must never be deleted by a read (review 2026-08-13).
* **Atomic persistence** — the ledger is written via tmp + ``os.replace`` so a
  crash mid-write can never truncate it to zero sources.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sources_path() -> Path:
    """Return the on-disk path of the blueprint-source registry JSON."""

    from clio_agent import paths  # noqa: PLC0415

    return paths.user_config_dir() / "agent-blueprint-sources.json"


def source_registry_id(source: str, ref: str = "") -> str:
    """Derive a stable ``src_*`` id from a source URL/path and optional ref."""

    digest = hashlib.sha256(f"{source}\n{ref}".encode("utf-8")).hexdigest()[:12]
    return f"src_{digest}"


def _is_prunable_dead_fixture(source: str) -> bool:
    """Whether a source row is dead LOCAL TEST RESIDUE (and only that).

    Three gates, all required: it parses as a local path (no URL scheme, no
    SCP-style ``host:path`` remote), the path does not exist, and it sits under
    a temp root (``pytest-of`` marker or the OS temp dir). Anything else —
    unreachable network drives, relative paths, remotes — is kept: gone-forever
    versus unreachable-right-now cannot be distinguished on a read.
    """

    if not source or "://" in source or source.startswith("git@"):
        return False
    # SCP-style remotes (host:path) have a colon NOT followed by a path
    # separator in position 2+ (Windows drive letters are `X:\` / `X:/`).
    colon = source.find(":")
    if colon > 1 or (colon == 1 and (len(source) < 3 or source[2] not in "\\/")):
        if colon != 1:
            return False
    path = Path(source).expanduser()
    if not path.is_absolute() or path.exists():
        return False
    lowered = source.lower().replace("\\", "/")
    temp_root = tempfile.gettempdir().lower().replace("\\", "/")
    return "pytest-of" in lowered or lowered.startswith(temp_root)


def load_agent_blueprint_sources() -> list[dict[str, Any]]:
    """Load the persisted blueprint-source rows (empty list if absent/corrupt).

    Self-heals dead test-fixture rows (see :func:`_is_prunable_dead_fixture`);
    the pruned ledger is rewritten atomically with a logged count.
    """

    path = sources_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/invalid sources file yields no rows
        return []
    rows = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    loaded = [dict(row) for row in rows if isinstance(row, dict)]
    kept = [row for row in loaded if not _is_prunable_dead_fixture(str(row.get("source") or ""))]
    pruned = len(loaded) - len(kept)
    if pruned:
        logger.warning(
            "blueprint_sources_pruned reason=dead_test_fixture count=%d file=%s",
            pruned,
            path,
        )
        save_agent_blueprint_sources(kept)
    return kept


def save_agent_blueprint_sources(rows: list[dict[str, Any]]) -> None:
    """Persist the blueprint-source rows atomically (tmp + ``os.replace``)."""

    path = sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"sources": rows}, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
