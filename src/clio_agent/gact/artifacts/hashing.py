"""Artifact identity hashing: stat-pin vs streamed sha256 (#1333 ratchet payment).

Split out of ``minting.py``: bounded streaming hashing of a designated output
path, config-first threshold, and the ``IdentityEvidence`` this produces. The
model is never load-bearing here. Re-exported from ``minting`` so every
existing import path (``from clio_agent.gact.artifacts.minting import
compute_identity`` / ``hash_max_file_bytes``, including the function-local
imports in ``transform_edges.py`` / ``versions.py`` and the
``clio_agent.gact.artifacts.minting.compute_identity`` monkeypatch target in
``tests/test_gact/test_hpc_external_inputs.py``) keeps working unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from clio_agent import conf
from clio_agent.gact.artifacts.records import IdentityEvidence

#: Default ceiling on hashing a designated output at mint. Over this, the version
#: is recorded ``stat-pinned`` (typed, permanent) rather than paying multi-GB I/O
#: on the turn thread (design resolution 5b). Config-first (#985 conventions).
_DEFAULT_HASH_MAX_FILE_BYTES = 64 * 1024 * 1024

_HASH_CHUNK_BYTES = 1024 * 1024


def hash_max_file_bytes() -> int:
    """Resolve the mint-time hash size threshold (bytes) from config.

    ``artifacts.hash_max_file_bytes`` (env ``CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES``)
    — a designated output larger than this is stat-pinned, not hashed.
    """
    return conf.resolve(
        "artifacts.hash_max_file_bytes",
        env="CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES",
        default=_DEFAULT_HASH_MAX_FILE_BYTES,
        cast=conf.as_int,
    )


@dataclass(frozen=True)
class _StatHash:
    """A designated path's stat + (optional) streamed sha256."""

    exists: bool
    size_bytes: int
    mtime: float
    sha256: Optional[str]
    over_threshold: bool


def _stat_and_hash(path: Path, max_bytes: int) -> _StatHash:
    """Stat ``path`` and stream its sha256 unless it exceeds ``max_bytes``.

    Streaming keeps memory bounded on large scientific outputs. Over the
    threshold, the hash is skipped and ``over_threshold`` is set so the caller
    records a ``stat-pinned`` evidence class (typed, never a silent hash-skip).
    """
    stat = path.stat()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    if size > max_bytes:
        return _StatHash(True, size, mtime, None, True)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return _StatHash(True, size, mtime, digest.hexdigest(), False)


def compute_identity(path: str | Path, *, max_bytes: int | None = None) -> IdentityEvidence:
    """Build :class:`IdentityEvidence` for a designated output path.

    Hashes when the file is at or under the threshold (``hashed-at-use``); over
    it, records ``stat-pinned`` with size+mtime. The path must exist — a caller
    minting for a non-existent designated path is a designation error the caller
    handles (this raises ``FileNotFoundError``), never a silent skip.
    """
    resolved = Path(str(path))
    ceiling = hash_max_file_bytes() if max_bytes is None else max_bytes
    sh = _stat_and_hash(resolved, ceiling)
    if sh.over_threshold or sh.sha256 is None:
        return IdentityEvidence.stat_pinned(size_bytes=sh.size_bytes, mtime=sh.mtime)
    return IdentityEvidence.hashed_at_use(
        sha256=sh.sha256, size_bytes=sh.size_bytes, mtime=sh.mtime
    )
