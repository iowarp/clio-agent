"""Init-time capacity gate for clio-core file-backed CTE tiers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from clio_agent.arc import clio_core_file_capacity, storage
from clio_agent.arc.clio_core_file_capacity import (
    CLIO_CORE_FILE_CAPACITY_UNAVAILABLE,
    ClioCoreFileCapacityError,
    preflight_file_tier_capacity,
)
from clio_agent.arc.storage import LocalFSStore


@dataclass(frozen=True)
class _DiskUsage:
    """Minimal ``shutil.disk_usage`` result used by the focused unit tests."""

    total: int
    used: int
    free: int


def _usage(free_bytes: int) -> _DiskUsage:
    """Return deterministic filesystem capacity with ``free_bytes`` available."""

    total = 100 * 1024**3
    return _DiskUsage(total=total, used=total - free_bytes, free=free_bytes)


def _write_config(tmp_path: Path, capacities: tuple[str, ...]) -> tuple[Path, tuple[Path, ...]]:
    """Write one CTE core module with file tiers rooted under ``tmp_path``."""

    targets = tuple(tmp_path / f"storage-{index}.bin" for index in range(len(capacities)))
    tiers = "\n".join(
        (
            f'      - path: "{target.as_posix()}"\n'
            '        bdev_type: "file"\n'
            f'        capacity_limit: "{capacity}"\n'
            f"        score: {index}.0"
        )
        for index, (target, capacity) in enumerate(zip(targets, capacities, strict=True))
    )
    config = tmp_path / "cte.yaml"
    config.write_text(
        "compose:\n"
        "  - mod_name: clio_cte_core\n"
        "    pool_name: cte_main\n"
        '    pool_id: "512.0"\n'
        "    storage:\n"
        f"{tiers}\n",
        encoding="utf-8",
    )
    return config, targets


def test_preflight_rejects_file_tier_that_cannot_fit_with_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed 50 GiB-on-a-full-volume shape fails before daemon spawn."""

    config, targets = _write_config(tmp_path, ("50GB",))
    free_bytes = 256 * 1024**2
    monkeypatch.setattr(
        clio_core_file_capacity.shutil, "disk_usage", lambda _path: _usage(free_bytes)
    )

    with pytest.raises(ClioCoreFileCapacityError) as raised:
        preflight_file_tier_capacity(config)

    error = raised.value
    assert error.degradation_reason == CLIO_CORE_FILE_CAPACITY_UNAVAILABLE
    assert error.target_paths == targets
    assert error.capacity_bytes == 50 * 1024**3
    assert error.required_allocation_bytes == 50 * 1024**3
    assert error.free_bytes == free_bytes
    assert error.reserve_bytes == 1024**3
    assert "required_free_bytes=" in str(error)


def test_preflight_accepts_capacity_when_one_gib_reserve_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh tier passes when allocation plus the safety reserve fits exactly."""

    config, _targets = _write_config(tmp_path, ("2GB",))
    monkeypatch.setattr(
        clio_core_file_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(3 * 1024**3),
    )

    rows = preflight_file_tier_capacity(config)

    assert len(rows) == 1
    assert rows[0].required_allocation_bytes == 2 * 1024**3


def test_preflight_aggregates_file_tiers_on_the_same_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tiers cannot each spend the same filesystem's free-byte balance."""

    config, targets = _write_config(tmp_path, ("2GB", "2GB"))
    monkeypatch.setattr(
        clio_core_file_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(9 * 512 * 1024**2),
    )

    with pytest.raises(ClioCoreFileCapacityError) as raised:
        preflight_file_tier_capacity(config)

    assert raised.value.target_paths == targets
    assert raised.value.required_allocation_bytes == 4 * 1024**3


def test_preflight_reuses_a_fully_provisioned_node_backing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing full-size ``_node0`` bdev does not need a second allocation."""

    config, (target,) = _write_config(tmp_path, ("2MB",))
    backing = Path(f"{target}_node0")
    backing.write_bytes(b"\0" * (2 * 1024**2))
    monkeypatch.setattr(
        clio_core_file_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(1024**3),
    )

    (row,) = preflight_file_tier_capacity(config)

    assert row.backing_path == backing
    assert row.existing_bytes == 2 * 1024**2
    assert row.required_allocation_bytes == 0


def test_preflight_rejects_existing_undersized_node_backing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clio-core reuses non-empty files, so an undersized one is a loud config error."""

    config, (target,) = _write_config(tmp_path, ("2MB",))
    backing = Path(f"{target}_node0")
    backing.write_bytes(b"short")
    monkeypatch.setattr(
        clio_core_file_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(10 * 1024**3),
    )

    with pytest.raises(ClioCoreFileCapacityError, match="smaller than capacity_limit"):
        preflight_file_tier_capacity(config)


def test_factory_loudly_degrades_before_clio_core_spawn_on_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capacity error reaches the typed init-degradation record and LocalFS."""

    from clio_agent.arc.init_degradation import (
        arc_init_degradation_snapshot,
        reset_arc_init_degradation,
    )

    config, _targets = _write_config(tmp_path, ("50GB",))
    monkeypatch.setattr(
        clio_core_file_capacity.shutil,
        "disk_usage",
        lambda _path: _usage(256 * 1024**2),
    )

    def unexpected_store(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ClioCoreStore must not initialize after capacity preflight fails")

    monkeypatch.setattr(storage, "ClioCoreStore", unexpected_store)
    reset_arc_init_degradation()
    try:
        store = storage.make_arc_store(
            backend="cte", data_dir=tmp_path / "arc-local", config_path=str(config)
        )
        record = arc_init_degradation_snapshot()
        assert isinstance(store, LocalFSStore)
        assert record is not None
        assert record.reason == CLIO_CORE_FILE_CAPACITY_UNAVAILABLE
        assert record.error_type == "ClioCoreFileCapacityError"
        assert "capacity_bytes=" in record.error
    finally:
        reset_arc_init_degradation()
