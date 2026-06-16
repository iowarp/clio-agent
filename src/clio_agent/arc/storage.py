"""IOWarp CTE (Convergent Tiered Environment) storage backend for ARC.

Integrates ARC Memory with IOWarp's multi-tier storage system for automatic
data migration across tiers based on access patterns.

Architecture:
    - Hot tier: In-memory cache (handled by LRUCache in memory.py)
    - Warm tier: SSD/local disk (default for active data)
    - Cold tier: Network storage/HDF5 (for historical data)
    - Archive tier: Tape/long-term storage (for old data)

Tier Migration Policy:
    - Hot → Warm: 1 day (handled by LRU cache eviction)
    - Warm → Cold: 7 days (infrequent access)
    - Cold → Archive: 30 days (historical data)

Graceful Degradation:
    If IOWarp is unavailable, falls back to local filesystem storage.

See PLAN.md v0.3.0 Task 2 for requirements.
"""

import base64
import logging
import os
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# The logical record families ARC persists. Each maps to one physical
# container in a store (a directory for LocalFSStore; a namespace/key prefix
# for a CTE-backed store). Keep this list as the single source of truth.
ARC_KINDS: tuple[str, ...] = (
    "conversations",
    "invocations",
    "metrics",
    "context",
    "profiles",
    "procedural",
    "variants",
    "segments",  # live context plane: one record per (session_id, scope)
)


@runtime_checkable
class ARCStore(Protocol):
    """Narrow persistence seam for ARC's record kinds.

    A record is addressed by ``(kind, name)``: ``kind`` is one of
    :data:`ARC_KINDS`; ``name`` is the record stem (no extension). The store
    owns the physical layout and tiering, so ARC never touches the filesystem
    directly. :class:`LocalFSStore` writes ``<data_dir>/<kind>/<name>.msgpack``;
    a clio-core CTE backend maps the same ``(kind, name)`` onto namespaced,
    multi-tier storage. This Protocol is the seam where that backend plugs in.
    """

    def put(self, kind: str, name: str, data: bytes, *, tier: str = "warm") -> None:
        """Persist ``data`` for ``(kind, name)`` (overwrites)."""
        ...

    def get(self, kind: str, name: str) -> Optional[bytes]:
        """Return bytes for ``(kind, name)`` or ``None`` if absent."""
        ...

    def exists(self, kind: str, name: str) -> bool:
        """Return whether a record exists for ``(kind, name)``."""
        ...

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        """Yield ``(name, data)`` for every record in ``kind`` whose name
        starts with ``prefix`` (``""`` = all). Order is unspecified."""
        ...

    def delete(self, kind: str, name: str) -> None:
        """Delete the record for ``(kind, name)`` if present (no-op if absent)."""
        ...

    def clear(self) -> None:
        """Delete all persisted records across all kinds."""
        ...


class LocalFSStore:
    """Default :class:`ARCStore` backed by the local filesystem.

    Lays records out as ``<data_dir>/<kind>/<name>.msgpack`` -- the historical
    on-disk format ARC has always used, so existing data directories are read
    unchanged. This is the extraction of the filesystem code that previously
    lived inline in ``ARCMemory``; the LSM tree remains a separate subsystem.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._dirs: Dict[str, Path] = {kind: self.data_dir / kind for kind in ARC_KINDS}
        for directory in self._dirs.values():
            directory.mkdir(exist_ok=True)

    def _dir(self, kind: str) -> Path:
        try:
            return self._dirs[kind]
        except KeyError:
            raise ValueError(f"unknown ARC kind {kind!r}; expected one of {ARC_KINDS}") from None

    def put(self, kind: str, name: str, data: bytes, *, tier: str = "warm") -> None:
        (self._dir(kind) / f"{name}.msgpack").write_bytes(data)

    def get(self, kind: str, name: str) -> Optional[bytes]:
        path = self._dir(kind) / f"{name}.msgpack"
        if not path.exists():
            return None
        return path.read_bytes()

    def exists(self, kind: str, name: str) -> bool:
        return (self._dir(kind) / f"{name}.msgpack").exists()

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for path in self._dir(kind).glob(f"{prefix}*.msgpack"):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            yield path.stem, data

    def delete(self, kind: str, name: str) -> None:
        path = self._dir(kind) / f"{name}.msgpack"
        if path.exists():
            path.unlink()

    def clear(self) -> None:
        for directory in self._dirs.values():
            for path in directory.glob("*.msgpack"):
                path.unlink()

class CTEStore:
    """ARCStore backed by the in-process clio-core CTE runtime.

    Maps ``(kind, name)`` -> ``(CTE tag, CTE blob)``. msgpack payloads are
    base64-wrapped because CTE's ``GetBlob`` UTF-8-decodes in the C++ binding and
    raises on non-UTF-8 bytes. The runtime is **embedded in this process**
    (``chimaera_init(..., default_with_runtime=True)`` self-starts it) and dies with
    the interpreter — there is NO external ``clio_run`` daemon.

    DURABILITY: the default CTE config is a single DRAM tier (shared memory), so
    data lives only while the process is up. Cross-restart durability needs a
    ``file`` bdev + WAL replay in the CTE config (a follow-up). For durable storage
    today, select the LocalFS backend (``CLIO_ARC_STORE=local``).
    """

    _initialized = False  # process-global init guard (the runtime inits exactly once)
    _init_lock = threading.Lock()

    def __init__(
        self,
        *,
        config_path: str = "",
        log_level: str = "error",
        init_settle_s: float = 0.5,
    ) -> None:
        self._ensure_runtime(config_path, log_level, init_settle_s)
        import clio_cte_core_ext as cte  # noqa: PLC0415

        self._cte = cte
        self._client = cte.get_cte_client()
        logger.info(
            "CTEStore active: clio-core CTE is the ARC backend (in-process runtime). "
            "The DEFAULT config is DRAM-only (not durable across restarts); durable + "
            "fault-tolerant tiers (file bdev, replication, erasure coding) are configured "
            "in the CTE config via CLIO_ARC_STORE_CONFIG. Use CLIO_ARC_STORE=local for "
            "disk durability today."
        )

    @classmethod
    def _ensure_runtime(cls, config_path: str, log_level: str, settle_s: float) -> None:
        """Boot the embedded CTE runtime exactly once per process."""
        with cls._init_lock:
            if cls._initialized:
                return
            os.environ.setdefault("CTP_LOG_LEVEL", log_level)
            # Import order is load-bearing: iowarp_core does the RTLD_GLOBAL .so
            # preload + seeds ~/.clio/clio.yaml; it MUST precede clio_cte_core_ext.
            # isort:skip keeps ruff from reordering these alphabetically.
            import iowarp_core  # noqa: F401, PLC0415  # isort:skip
            import clio_cte_core_ext as cte  # noqa: PLC0415  # isort:skip

            devnull = os.open(os.devnull, os.O_WRONLY)
            saved_stderr = os.dup(2)
            os.dup2(devnull, 2)  # silence the C++ fd-level startup banner
            try:
                cte.chimaera_init(cte.ChimaeraMode.kClient, True)  # True => embedded runtime
                time.sleep(settle_s)  # let the co-process spin up
                cte.initialize_cte(config_path, cte.PoolQuery.Dynamic())  # "" => ~/.clio/clio.yaml
            finally:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
                os.close(devnull)
            cls._initialized = True
            logger.info("CTE embedded runtime initialized (no external daemon)")

    # ---- ARCStore Protocol ----

    def put(self, kind: str, name: str, data: bytes, *, tier: str = "warm") -> None:
        # base64-wrap: CTE GetBlob UTF-8-decodes, so store ascii-safe bytes.
        self._cte.Tag(kind).PutBlob(name, base64.b64encode(data), 0)
        # ``tier`` is advisory: the default single DRAM tier makes ReorganizeBlob a
        # no-op. Wire tier->score only when a real file/HDD bdev is configured.

    def get(self, kind: str, name: str) -> Optional[bytes]:
        tag = self._cte.Tag(kind)
        size = tag.GetBlobSize(name)  # 0 for a missing blob (does not raise)
        if size == 0:
            return None
        return base64.b64decode(tag.GetBlob(name, size, 0))

    def exists(self, kind: str, name: str) -> bool:
        return self._cte.Tag(kind).GetBlobSize(name) > 0

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        tag = self._cte.Tag(kind)
        for blob_name in tag.GetContainedBlobs():
            if blob_name.startswith(prefix):
                value = self.get(kind, blob_name)
                if value is not None:
                    yield blob_name, value

    def delete(self, kind: str, name: str) -> None:
        # Tag has no per-blob delete; go through the Client + TagId. DelBlob on a
        # missing blob returns False (no raise), satisfying the no-op contract.
        tag = self._cte.Tag(kind)
        self._client.DelBlob(tag.GetTagId(), name)

    def clear(self) -> None:
        for kind in ARC_KINDS:
            tag = self._cte.Tag(kind)
            tag_id = tag.GetTagId()
            for blob_name in tag.GetContainedBlobs():
                self._client.DelBlob(tag_id, blob_name)


def make_arc_store(
    *,
    backend: Optional[str] = None,
    data_dir: "str | Path" = ".clio_agent/arc",
    config_path: str = "",
) -> "ARCStore":
    """Build the ARC persistence backend.

    Selection (first match wins):
        1. explicit ``backend`` arg ("cte" | "local")
        2. env ``CLIO_ARC_STORE`` ("cte" | "local")
        3. default ``"cte"`` (clio-core CTE, in-process; the gold-standard backend)

    Graceful degradation (CLAUDE.md): if the CTE binding is absent or the runtime
    fails to init, fall back to ``LocalFSStore`` with a warning — never crash.
    Note: the default CTE config is DRAM-only (not durable across restarts); use
    ``CLIO_ARC_STORE=local`` for disk-durable storage until a file tier is wired.
    """
    choice = (backend or os.environ.get("CLIO_ARC_STORE", "cte")).strip().lower()
    if choice == "local":
        return LocalFSStore(data_dir)
    if choice == "cte":
        cfg = config_path or os.environ.get("CLIO_ARC_STORE_CONFIG", "")
        try:
            return CTEStore(config_path=cfg)
        except Exception as exc:  # noqa: BLE001 - binding absent or init failure
            warnings.warn(
                f"CTE store unavailable ({exc}); falling back to LocalFSStore",
                RuntimeWarning,
                stacklevel=2,
            )
            logger.warning("CTE store unavailable (%s); using LocalFSStore", exc)
            return LocalFSStore(data_dir)
    raise ValueError(f"unknown CLIO_ARC_STORE {choice!r}; expected 'cte' or 'local'")
