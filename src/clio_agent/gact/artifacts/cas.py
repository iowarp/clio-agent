"""Content-addressed store (CAS) — the ONLY new artifact storage (owner decision #966.8).

Small artifacts (plots, reports, scripts) get ``cas`` custody: their bytes are
copied into an app-owned, sha256-addressed store so they survive workspace churn
(a deleted/overwritten workspace file) and self-validate on read. Big datasets
stay ``workspace-referenced`` / ``external-referenced`` with a typed
``not_ingested_size`` reason — clio never pays multi-GB copies (design resolution
5b, #930-class discipline).

Layout (owner decision #966.8)::

    <workspace_root>/.clio/agent/artifacts/cas/<sha[:2]>/<sha>

keyed by content hash, so the SAME content under two names is ONE blob (natural
dedup; the "refcount" is the number of registered versions pointing at the sha —
a registry query over the version chains, never a stored counter). A blob is
written temp+rename keyed by its hash: idempotent, so a double-ingest of the same
content is a verify-and-skip, never a torn file.

Ingestion **tees the identity hash** (owner decision #966.8, this slice's item 1):
the mint already streams the file once to compute its sha (``compute_identity`` in
:mod:`minting`); :func:`ingest_identity` streams that SAME read and, for a file at
or under ``artifacts.cas_max_file_bytes``, writes each chunk through to the CAS
temp file as it hashes — zero extra full read.

This module is pure store + ingest + knobs; reachability GC + budget enforcement
live in the sibling :mod:`cas_gc` (no-accretion).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from clio_agent import conf, paths
from clio_agent.gact.artifacts.records import Custody, IdentityEvidence

logger = logging.getLogger(__name__)

#: Ceiling on a version's size for CAS ingestion (owner decision #966.8). At or
#: under this the bytes are copied into CAS (custody ``cas``); over it the version
#: stays referenced with a typed ``not_ingested_size``. Config-first (#985).
_DEFAULT_CAS_MAX_FILE_BYTES = 16 * 1024 * 1024  # 16 MiB
#: The CAS store's total-size budget (owner decision #966.8, #930-class ratchet).
#: Over it, the reachability GC (:mod:`cas_gc`) evicts unreachable blobs oldest-first.
_DEFAULT_CAS_BUDGET_BYTES = 512 * 1024 * 1024  # 512 MiB
#: Whether to TRUST a stat (size+mtime) when confirming an already-present blob.
#: Default ``False`` — Windows mtimes are unreliable (owner decision #966.8 / key
#: risk), so a custody-critical existing blob is re-hashed, not trusted-by-stat.
_DEFAULT_HASH_STAT_CACHE = False

_HASH_CHUNK_BYTES = 1024 * 1024


def cas_max_file_bytes() -> int:
    """Resolve the per-file CAS ingestion ceiling (bytes) from config.

    ``artifacts.cas_max_file_bytes`` (env ``CLIO_ARTIFACT_CAS_MAX_FILE_BYTES``).
    """
    return conf.resolve(
        "artifacts.cas_max_file_bytes",
        env="CLIO_ARTIFACT_CAS_MAX_FILE_BYTES",
        default=_DEFAULT_CAS_MAX_FILE_BYTES,
        cast=conf.as_int,
    )


def cas_budget_bytes() -> int:
    """Resolve the CAS store byte budget from config.

    ``artifacts.cas_budget_bytes`` (env ``CLIO_ARTIFACT_CAS_BUDGET_BYTES``).
    """
    return conf.resolve(
        "artifacts.cas_budget_bytes",
        env="CLIO_ARTIFACT_CAS_BUDGET_BYTES",
        default=_DEFAULT_CAS_BUDGET_BYTES,
        cast=conf.as_int,
    )


def hash_stat_cache() -> bool:
    """Whether to trust a stat when confirming a present blob (default: distrust).

    ``artifacts.hash_stat_cache`` (env ``CLIO_ARTIFACT_HASH_STAT_CACHE``). Default
    ``False`` re-hashes an existing blob to confirm it still matches its address
    (self-validation) rather than trusting an unreliable mtime/size.
    """
    return conf.resolve(
        "artifacts.hash_stat_cache",
        env="CLIO_ARTIFACT_HASH_STAT_CACHE",
        default=_DEFAULT_HASH_STAT_CACHE,
        cast=conf.as_bool,
    )


def cas_root_for(workspace_root: str | Path) -> Path:
    """The CAS store root for a workspace: ``<root>/.clio/agent/artifacts/cas``."""
    return paths.workspace_agent_dir(workspace_root) / "artifacts" / "cas"


@dataclass(frozen=True)
class BlobEntry:
    """One present CAS blob: its content hash, path, size and mtime."""

    sha256: str
    path: Path
    size_bytes: int
    mtime: float


class CASStore:
    """The sha256-addressed blob store rooted under one workspace.

    Content-addressed: :meth:`blob_path` maps a hash to ``<root>/<sha[:2]>/<sha>``.
    Writes are temp+rename keyed by the hash so an ingest is idempotent and never
    leaves a torn file; a present blob is confirmed (re-hashed unless the stat cache
    is trusted) and self-healed from the fresh bytes on corruption.
    """

    def __init__(self, workspace_root: str | Path) -> None:
        self._root = cas_root_for(workspace_root)

    @property
    def root(self) -> Path:
        """The CAS store root directory."""
        return self._root

    def blob_path(self, sha256: str) -> Path:
        """Map a content hash to its addressed blob path (``<root>/<sha[:2]>/<sha>``)."""
        return self._root / sha256[:2] / sha256

    def has_blob(self, sha256: str) -> bool:
        """Whether a blob for ``sha256`` is present on disk."""
        return bool(sha256) and self.blob_path(sha256).is_file()

    def _tmp_dir(self) -> Path:
        tmp = self._root / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    def finalize_temp(
        self, tmp_path: Path, sha256: str, size_bytes: int, *, trust_stat: bool
    ) -> tuple[Path, str]:
        """Publish a freshly-hashed temp file to its addressed blob path.

        Returns ``(blob_path, reason)``. Idempotent: when the addressed blob is
        already present it is CONFIRMED — re-hashed (unless ``trust_stat`` and the
        size matches) — and the temp discarded (``dedup_existing``); a corrupt
        present blob is SELF-HEALED by replacing it with the fresh temp bytes
        (``self_healed``). Otherwise the temp is atomically renamed into place
        (``ingested``).
        """
        blob = self.blob_path(sha256)
        if blob.is_file():
            if self._blob_valid(blob, sha256, size_bytes, trust_stat=trust_stat):
                _silent_unlink(tmp_path)
                return (blob, "dedup_existing")
            # Present but corrupt (bit-rot / a truncated prior write) — replace it
            # with the fresh, verified temp bytes rather than trust the bad blob.
            logger.warning(
                "cas blob corrupt on ingest reason=cas_blob_self_healed sha=%s path=%s",
                sha256,
                blob,
            )
            blob.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, blob)
            return (blob, "self_healed")
        blob.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, blob)
        return (blob, "ingested")

    def _blob_valid(self, blob: Path, sha256: str, size_bytes: int, *, trust_stat: bool) -> bool:
        """Confirm a present blob still matches its address.

        With the stat cache trusted, a size match suffices; otherwise (the default,
        Windows-safe) the blob is re-hashed and compared to its address.
        """
        try:
            if trust_stat:
                return blob.stat().st_size == size_bytes
            return sha256_file(blob) == sha256
        except OSError:
            return False

    def total_bytes(self) -> int:
        """Sum the on-disk size of every published blob (the ``.tmp`` scratch aside)."""
        total = 0
        for entry in self.iter_blobs():
            total += entry.size_bytes
        return total

    def iter_blobs(self) -> Iterator[BlobEntry]:
        """Yield every published blob as a :class:`BlobEntry` (``.tmp`` excluded)."""
        if not self._root.is_dir():
            return
        for shard in sorted(self._root.iterdir()):
            if not shard.is_dir() or shard.name == ".tmp":
                continue
            for blob in sorted(shard.iterdir()):
                if not blob.is_file():
                    continue
                try:
                    stat = blob.stat()
                except OSError:
                    continue
                yield BlobEntry(
                    sha256=blob.name,
                    path=blob,
                    size_bytes=int(stat.st_size),
                    mtime=float(stat.st_mtime),
                )

    def evict(self, sha256: str) -> int:
        """Delete a blob, returning the bytes freed (0 when it was already absent)."""
        blob = self.blob_path(sha256)
        try:
            size = blob.stat().st_size
        except OSError:
            return 0
        _silent_unlink(blob)
        return int(size)


@dataclass(frozen=True)
class IngestedIdentity:
    """Identity + custody decision for a designated output, teed through CAS.

    The ONE value the mint seams read: ``evidence`` is the (streamed) identity;
    ``custody`` is ``cas`` when the bytes were ingested, else the referenced class;
    ``not_ingested_size`` is the typed marker on an over-threshold version;
    ``reason`` is the machine tag for the decision (never a silent fallback).
    """

    evidence: IdentityEvidence
    custody: Custody
    reason: str
    not_ingested_size: Optional[int] = None
    blob_path: Optional[Path] = None


def sha256_file(path: str | Path) -> str:
    """Stream a file's sha256 (bounded memory). Raises ``OSError`` on read failure."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _stream_hash_tee(path: Path, tmp_handle) -> tuple[str, int]:
    """Stream ``path`` once, updating the sha AND writing each chunk to ``tmp_handle``.

    The tee (owner decision #966.8 item 1): the identity hash and the CAS write
    share the SAME read — zero extra full read. Returns ``(sha256, size)``.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as src:
        while True:
            chunk = src.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            tmp_handle.write(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def ingest_identity(
    path: str | Path,
    *,
    workspace_root: Optional[str | Path],
    cas_max_bytes: Optional[int] = None,
    hash_max_bytes: Optional[int] = None,
    trust_stat: Optional[bool] = None,
) -> IngestedIdentity:
    """Compute a designated output's identity and, when small, ingest it into CAS.

    Streams the file ONCE (:func:`_stream_hash_tee`) for a size at or under
    ``cas_max_bytes`` with a resolvable CAS root — teeing the identity hash straight
    into the addressed blob (custody ``cas``). Larger-but-hashable files hash
    without ingesting (custody referenced, typed ``not_ingested_size``); a file over
    the hash ceiling is ``stat-pinned`` and never read whole (typed
    ``over_hash_threshold``). Every degraded/referenced path carries a typed reason
    (no silent fallback). Raises ``OSError`` only on a stat failure — the caller
    handles a designation error, never a silent skip.
    """
    resolved = Path(str(path))
    stat = resolved.stat()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)

    if hash_max_bytes is None:
        from clio_agent.gact.artifacts.minting import hash_max_file_bytes  # noqa: PLC0415

        hash_max_bytes = hash_max_file_bytes()
    if size > hash_max_bytes:
        # Over the hash ceiling — never read whole; stat-pinned, referenced.
        return IngestedIdentity(
            evidence=IdentityEvidence.stat_pinned(size_bytes=size, mtime=mtime),
            custody=Custody.WORKSPACE_REFERENCED,
            reason="over_hash_threshold",
            not_ingested_size=size,
        )

    cas_max = cas_max_file_bytes() if cas_max_bytes is None else cas_max_bytes
    can_ingest = workspace_root is not None and size <= cas_max
    if not can_ingest:
        sha = sha256_file(resolved)
        evidence = IdentityEvidence.hashed_at_use(sha256=sha, size_bytes=size, mtime=mtime)
        if size > cas_max:
            # Hashed, but too big for CAS — referenced with a typed size reason.
            return IngestedIdentity(
                evidence=evidence,
                custody=Custody.WORKSPACE_REFERENCED,
                reason="not_ingested_size",
                not_ingested_size=size,
            )
        # Small enough, but no CAS root resolvable — referenced, typed (never silent).
        return IngestedIdentity(
            evidence=evidence,
            custody=Custody.WORKSPACE_REFERENCED,
            reason="cas_store_unavailable",
        )

    assert workspace_root is not None
    store = CASStore(workspace_root)
    trust = hash_stat_cache() if trust_stat is None else trust_stat
    tmp_dir = store._tmp_dir()
    fd, tmp_name = tempfile.mkstemp(dir=str(tmp_dir), prefix="ingest-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp_handle:
            sha, streamed = _stream_hash_tee(resolved, tmp_handle)
        blob, reason = store.finalize_temp(tmp_path, sha, streamed, trust_stat=trust)
    except OSError:
        _silent_unlink(tmp_path)
        raise
    evidence = IdentityEvidence.hashed_at_use(sha256=sha, size_bytes=streamed, mtime=mtime)
    return IngestedIdentity(
        evidence=evidence,
        custody=Custody.CAS,
        reason=reason,
        blob_path=blob,
    )


def _silent_unlink(path: Path) -> None:
    """Remove ``path`` if present; a missing file is a no-op (never raises)."""
    try:
        path.unlink()
    except OSError:
        pass


__all__ = [
    "BlobEntry",
    "CASStore",
    "IngestedIdentity",
    "cas_budget_bytes",
    "cas_max_file_bytes",
    "cas_root_for",
    "hash_stat_cache",
    "ingest_identity",
    "sha256_file",
]
