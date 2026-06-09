"""Human-curated context-window "marketplace" source.

The marketplace is the middle tier of the factory's strict order
(models.dev -> **marketplace** -> static). It exists for models that models.dev
does not know — typically site-local or freshly-deployed open-weights checkpoints
whose real serving window an operator knows but no public catalog lists yet.

It is a flat ``{normalized_id: context_window}`` mapping merged from two layers:

1. a small **built-in seed** that ships with clio, and
2. an optional on-disk **DB file** (JSON) which, when present, *overrides* the
   seed. The DB path comes from the ``CLIO_CONTEXT_DB`` environment variable, or
   a default ``context_db.json`` under the clio config dir.

The DB file is plain JSON: either a flat ``{"<id>": <int>}`` object or an object
with a top-level ``"models"`` key holding that mapping (so the file can carry
metadata alongside). Ids are normalized on load; malformed rows are skipped
rather than raising, so a hand-edited DB can never break a handshake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from clio_agent.providers.handshake.sources._normalize import (
    iter_id_candidates,
    normalize_id,
)

#: Environment variable naming an explicit marketplace DB file.
CONTEXT_DB_ENV = "CLIO_CONTEXT_DB"

#: Built-in seed: a few curated windows for models commonly run against clio that
#: public catalogs may lag on. Keys are already normalized. These are
#: deployment-known windows for checkpoints that models.dev does not (yet) list.
_SEED_MARKETPLACE: dict[str, int] = {
    "granite-4-h-tiny": 1_048_576,
    "qwopus3.5-9b-v3": 262_144,
}


def _config_dir() -> Path:
    """Return the clio config dir, honouring ``XDG_CONFIG_HOME`` like the rest of clio."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if base:
        return Path(base) / "clio-agent"
    return Path.home() / ".config" / "clio-agent"


def default_db_path() -> Path:
    """Return the default marketplace DB path under the config dir."""
    return _config_dir() / "context_db.json"


def resolve_db_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the marketplace DB path.

    Precedence: an explicit ``path`` argument, then the ``CLIO_CONTEXT_DB``
    environment variable, then the config-dir default.

    Args:
        path: Optional explicit path (test seam / caller override).

    Returns:
        The resolved (not necessarily existing) DB path.
    """
    if path is not None:
        return Path(path)
    env = os.environ.get(CONTEXT_DB_ENV, "").strip()
    if env:
        return Path(env)
    return default_db_path()


def _coerce_window(value: object) -> int | None:
    """Coerce a raw DB value to a positive context window, or None if invalid."""
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _load_db_file(path: Path) -> dict[str, int]:
    """Load and normalize a marketplace DB file; return {} on any read/parse failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}

    # Accept either a flat mapping or an object wrapping it under "models".
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        rows = data["models"]
    elif isinstance(data, dict):
        rows = data
    else:
        return {}

    out: dict[str, int] = {}
    for key, value in rows.items():
        if not isinstance(key, str):
            continue
        norm = normalize_id(key)
        window = _coerce_window(value)
        if norm and window is not None:
            out[norm] = window
    return out


def load_marketplace(path: str | os.PathLike[str] | None = None) -> dict[str, int]:
    """Return the merged marketplace mapping (seed overlaid by the DB file, if any).

    Args:
        path: Optional explicit DB path; otherwise resolved via env/default.

    Returns:
        A normalized ``{id: context_window}`` mapping. Always includes the seed;
        on-disk entries override seed entries with the same id.
    """
    merged = dict(_SEED_MARKETPLACE)
    db_path = resolve_db_path(path)
    if db_path.exists():
        merged.update(_load_db_file(db_path))
    return merged


def lookup_marketplace(model_id: str, *, path: str | os.PathLike[str] | None = None) -> int | None:
    """Return the curated context window for ``model_id``, or None.

    Tries each normalized candidate form of the id against the merged mapping.

    Args:
        model_id: The raw model identifier (may include a ``vendor/`` prefix).
        path: Optional explicit DB path (test seam).

    Returns:
        The context window in tokens, or ``None`` on a miss.
    """
    mapping = load_marketplace(path)
    for candidate in iter_id_candidates(model_id):
        window = mapping.get(candidate)
        if window is not None:
            return window
    return None
