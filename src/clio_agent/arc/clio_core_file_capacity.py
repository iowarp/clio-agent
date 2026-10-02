"""Init-time filesystem-capacity gate for clio-core CTE file tiers.

clio-core grows a file tier's backing file in 1 GiB chunks on every platform, so the
configured ``capacity_limit`` is a growth ceiling, not an up-front allocation. Measured on
iowarp-core 2.2.1 (Windows, 2026-10-01): a 3 GB and a 6 GB tier both started at 1 GiB, and
writing ~1.4 GB through the store grew the file to 2 GiB with every blob read back. (The
earlier rule -- Windows allocates the full capacity, an existing smaller file is never
grown -- refused a store clio-core was serving, on its second start.) So a fresh tier needs
its first chunk plus a free-space reserve, and an existing backing file is reused as is.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from clio_agent.arc.clio_core_config import RamTierCap, boot_check_ram_cap, parse_capacity_bytes
from clio_agent.arc.init_degradation import CLIO_CORE_FILE_CAPACITY_UNAVAILABLE

logger = logging.getLogger(__name__)

# Preserve capacity for CTE's 32 MiB transaction log, ARC metadata, and normal host
# operation. A fixed GiB is deterministic and does not grow with an archival tier.
_FILE_TIER_FREE_SPACE_RESERVE_BYTES = 1 * 1024**3


# clio-core's file-tier growth unit: the first chunk is all a fresh tier allocates.
_FILE_TIER_GROWTH_CHUNK_BYTES = 1 * 1024**3


class ClioCoreFileCapacityError(RuntimeError):
    """A configured CTE file tier cannot be allocated safely on its filesystem."""

    degradation_reason = CLIO_CORE_FILE_CAPACITY_UNAVAILABLE

    def __init__(
        self,
        *,
        config_path: Path,
        filesystem_path: Path,
        target_paths: tuple[Path, ...],
        capacity_bytes: int,
        required_allocation_bytes: int,
        existing_bytes: int,
        free_bytes: int,
        reserve_bytes: int,
    ) -> None:
        """Build a typed, operator-actionable capacity failure.

        Args:
            config_path: Exact CTE YAML file being initialized.
            filesystem_path: Existing path used to query filesystem capacity.
            target_paths: Configured file-tier targets on that filesystem.
            capacity_bytes: Sum of configured capacities for those targets.
            required_allocation_bytes: Bytes that fresh backing files must allocate.
            existing_bytes: Bytes already provisioned by reusable backing files.
            free_bytes: Filesystem free bytes observed by the preflight.
            reserve_bytes: Safety reserve that must remain after allocation.
        """

        self.config_path = config_path
        self.filesystem_path = filesystem_path
        self.target_paths = target_paths
        self.capacity_bytes = capacity_bytes
        self.required_allocation_bytes = required_allocation_bytes
        self.existing_bytes = existing_bytes
        self.free_bytes = free_bytes
        self.reserve_bytes = reserve_bytes
        required_free_bytes = required_allocation_bytes + reserve_bytes
        targets = ", ".join(str(path) for path in target_paths)
        super().__init__(
            "clio-core file-tier capacity preflight failed "
            f"(reason={self.degradation_reason} config={config_path} "
            f"filesystem={filesystem_path} targets=[{targets}] "
            f"capacity_bytes={capacity_bytes} "
            f"required_allocation_bytes={required_allocation_bytes} "
            f"existing_bytes={existing_bytes} free_bytes={free_bytes} "
            f"reserve_bytes={reserve_bytes} required_free_bytes={required_free_bytes}); "
            "choose a file-tier path on a filesystem with sufficient free space or lower "
            "arc.cte.file_capacity / CLIO_ARC_CTE_FILE_CAPACITY"
        )


@dataclass(frozen=True)
class FileTierCapacity:
    """One configured CTE file tier resolved to its local backing filesystem."""

    target_path: Path
    backing_path: Path
    filesystem_path: Path
    filesystem_device: int
    capacity_bytes: int
    existing_bytes: int
    required_allocation_bytes: int


def _closest_existing_directory(path: Path) -> Path:
    """Return the closest existing directory at or above ``path``."""

    candidate = path
    while True:
        if candidate.is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing filesystem ancestor for CTE tier path {path}")
        candidate = parent


def _configured_file_tiers(config_path: Path) -> list[tuple[Path, int]]:
    """Parse configured ``file`` storage tiers as ``(path, capacity_bytes)`` rows.

    Relative paths retain clio-core's process-working-directory semantics. Invalid
    YAML remains the authoritative clio-core parser's responsibility; malformed
    capacities use :func:`parse_capacity_bytes`'s existing fail-loud error.
    """

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(raw, Mapping):
        return []
    compose = raw.get("compose", []) or []
    if not isinstance(compose, list):
        return []

    tiers: list[tuple[Path, int]] = []
    for module in compose:
        if not isinstance(module, Mapping):
            continue
        storage = module.get("storage", []) or []
        if not isinstance(storage, list):
            continue
        for tier in storage:
            if not isinstance(tier, Mapping):
                continue
            if str(tier.get("bdev_type", "")).strip().lower() != "file":
                continue
            raw_path = str(tier.get("path", "")).strip()
            capacity = tier.get("capacity_limit")
            if not raw_path or capacity is None:
                continue
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                target = Path.cwd() / target
            tiers.append((target.resolve(strict=False), parse_capacity_bytes(str(capacity))))
    return tiers


def inspect_file_tier_capacities(config_path: str | Path) -> tuple[FileTierCapacity, ...]:
    """Inspect allocation requirements for every configured CTE file tier.

    clio-core appends ``_node0`` to the configured path for the local daemon. A fresh
    tier allocates its first growth chunk (at most the capacity); an existing backing file
    -- usually smaller than the capacity, as clio-core grows it in chunks -- needs nothing.

    Args:
        config_path: Exact CTE YAML file being initialized.

    Returns:
        Immutable file-tier allocation facts.

    Raises:
        ValueError: If a configured capacity is malformed.
    """

    config = Path(config_path).expanduser().resolve(strict=False)
    rows: list[FileTierCapacity] = []
    for target, capacity_bytes in _configured_file_tiers(config):
        backing = Path(f"{target}_node0")
        filesystem_path = _closest_existing_directory(backing.parent)
        existing_bytes = backing.stat().st_size if backing.is_file() else 0
        first_chunk = min(capacity_bytes, _FILE_TIER_GROWTH_CHUNK_BYTES)
        rows.append(
            FileTierCapacity(
                target_path=target,
                backing_path=backing,
                filesystem_path=filesystem_path,
                filesystem_device=filesystem_path.stat().st_dev,
                capacity_bytes=capacity_bytes,
                existing_bytes=existing_bytes,
                required_allocation_bytes=0 if existing_bytes > 0 else first_chunk,
            )
        )
    return tuple(rows)


def preflight_file_tier_capacity(
    config_path: str | Path,
    *,
    reserve_bytes: int = _FILE_TIER_FREE_SPACE_RESERVE_BYTES,
) -> tuple[FileTierCapacity, ...]:
    """Fail before daemon spawn when configured file tiers cannot fit safely.

    Requirements are aggregated by filesystem, so multiple tiers cannot each spend
    the same free-byte balance.

    Args:
        config_path: Exact CTE YAML file being initialized.
        reserve_bytes: Bytes that must remain free after fresh allocations.

    Returns:
        Immutable file-tier allocation facts.

    Raises:
        ClioCoreFileCapacityError: If allocation plus reserve cannot fit.
        ValueError: If the reserve is negative or a capacity is malformed.
    """

    if reserve_bytes < 0:
        raise ValueError("CTE file-tier free-space reserve must be non-negative")
    config = Path(config_path).expanduser().resolve(strict=False)
    rows = inspect_file_tier_capacities(config)
    grouped: dict[int, list[FileTierCapacity]] = {}
    for row in rows:
        grouped.setdefault(row.filesystem_device, []).append(row)

    for group in grouped.values():
        filesystem_path = group[0].filesystem_path
        free_bytes = shutil.disk_usage(filesystem_path).free
        required = sum(row.required_allocation_bytes for row in group)
        if required + reserve_bytes > free_bytes:
            raise ClioCoreFileCapacityError(
                config_path=config,
                filesystem_path=filesystem_path,
                target_paths=tuple(row.target_path for row in group),
                capacity_bytes=sum(row.capacity_bytes for row in group),
                required_allocation_bytes=required,
                existing_bytes=sum(row.existing_bytes for row in group),
                free_bytes=free_bytes,
                reserve_bytes=reserve_bytes,
            )
    return rows


def preflight_clio_core_config(config_path: str | Path, *, env: Mapping[str, str]) -> RamTierCap:
    """Run blocking file-capacity and warning-only RAM topology checks before CTE init.

    Args:
        config_path: Exact CTE YAML file being initialized.
        env: Environment mapping used by the RAM topology check.

    Returns:
        The inspected RAM topology facts.
    """

    preflight_file_tier_capacity(config_path)
    return boot_check_ram_cap(config_path, env=env)


_SEED_FRACTION_OF_FREE = 0.10
_SEED_MIN_GIB = 2
_SEED_MAX_GIB = 50


def seeded_file_capacity(target_dir: Path) -> str:
    """The file-tier capacity a fresh install seeds into ``target_dir``: 10% of that disk's
    free space, at least 2 GB and at most 50 GB.

    The capacity is the tier's growth ceiling (clio-core grows the file in 1 GiB chunks), so
    a ceiling the disk cannot hold would fail writes once the disk fills; a fixed 50 GB
    promised more than a small disk has. A disk too small even for the floor fails the
    preflight, typed.
    """
    free = shutil.disk_usage(_closest_existing_directory(target_dir)).free
    gib = int(free * _SEED_FRACTION_OF_FREE) // (1 << 30)
    return f"{min(max(gib, _SEED_MIN_GIB), _SEED_MAX_GIB)}GB"


_OLD_FIXED_SEED = "50GB"


def ensure_seeded_capacity_fits(config_path: Path, capacity: str) -> None:
    """Resize CLIO's own seed from before the disk-based sizing, once, when it cannot run.

    A config seeded with the fixed 50 GB whose single file tier is not allocated yet and
    does not fit its disk is rewritten in place to ``capacity`` (the seed rule, or an
    explicit ``arc.cte.file_capacity``) and logged. An allocated tier, a config that fits, and any
    other capacity value (a user's choice) are left alone; the preflight still judges them.
    """
    text = config_path.read_text(encoding="utf-8")
    old = f'capacity_limit: "{_OLD_FIXED_SEED}"'
    tiers = _configured_file_tiers(config_path)
    if text.count(old) != 1 or len(tiers) != 1:
        return
    target, capacity_bytes = tiers[0]
    backing = Path(f"{target}_node0")  # clio-core's local backing file
    if backing.exists():
        return
    free = shutil.disk_usage(_closest_existing_directory(backing.parent)).free
    if capacity_bytes + _FILE_TIER_FREE_SPACE_RESERVE_BYTES <= free:
        return
    config_path.write_text(text.replace(old, f'capacity_limit: "{capacity}"'), encoding="utf-8")
    logger.warning(
        "clio-core config %s: the old fixed %s file tier does not fit its disk (%d bytes "
        "free) and is not allocated yet; resized in place to %s",
        config_path,
        _OLD_FIXED_SEED,
        free,
        capacity,
    )
