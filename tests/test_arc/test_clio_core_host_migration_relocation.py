"""The host-key migration moves the whole store's record of its location, not only cte.yaml.

Before, only ``cte.yaml`` was rewritten: clio-core's pool log and file bdev model still
named the old ``<data>/cte``, and the capacity journal kept the digest of the old
config, so managed growth read the moved config as a person's edit and stopped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from pathlib import Path

import pytest

from clio_agent import path_relocation
from clio_agent.arc import clio_core_config, clio_core_relocate
from clio_agent.arc.clio_core_host_migration import (
    CTE_STORE_MIGRATED,
    CTE_STORE_MIGRATION_RELOCATION_INCOMPLETE,
    migrate_legacy_cte_store,
)
from clio_agent.arc.clio_core_relocate import CTE_POOL_LOG_NOT_RELOCATED, wal_texts

from .test_clio_core_host_migration import _legacy_store, home  # noqa: F401 - fixture


@pytest.fixture(autouse=True)
def _own_relocation_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process's relocation records (served by /v1/health) stay each test's."""
    monkeypatch.setattr(path_relocation, "_records", [])
    monkeypatch.setattr(clio_core_relocate, "_last", None)


def _u64(text: str) -> bytes:
    return struct.pack("<Q", len(text.encode())) + text.encode()


def _record(major: int, pool: str, chimod: str, params: bytes) -> bytes:
    out = b"\x01\x01" + struct.pack("<Q", major)
    for field in (pool.encode(), chimod.encode(), params):
        out += struct.pack("<I", len(field)) + field
    return out


def _with_clio_core_records(legacy: Path) -> None:
    """What clio-core 2.4 and CLIO's growth leave beside the seed after a run."""
    here = legacy.as_posix()
    core = (
        f"mod_name: clio_cte_core\nstorage:\n  - path: {here}/storage.bin\nperformance:\n"
        f"  metadata_log_path: {here}/metadata.log\n  transaction_log_capacity: 32MB"
    )
    (legacy / "conf" / "wal").mkdir()
    (legacy / "conf" / "wal" / "pools.0.bin").write_bytes(
        _record(512, "cte_main", "clio_cte_core", _u64("cte_main") + b"\x00\x02\x00" + _u64(core))
        + _record(
            564,
            "clio_cte_indexer",
            "clio_cte_indexer",
            _u64(f"index_log_path: {here}/indexer_index.log"),
        )
    )
    (legacy / "conf" / "models").mkdir()
    model = f"clio_bdev.{here.replace('/', '_')}_storage.bin_node0.0.0.yaml"
    (legacy / "conf" / "models" / model).write_text(
        f"chimod_name: clio_bdev\npool_name: {here}/storage.bin_node0\n", encoding="utf-8"
    )
    digest = hashlib.sha256((legacy / "cte.yaml").read_bytes()).hexdigest()
    (legacy / "cte-capacity.json").write_text(json.dumps({"schema": 1, "config_sha256": digest}))


def test_the_moved_pool_log_model_and_journal_name_this_hosts_dir(
    home: Path,  # noqa: F811 - the fixture
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy = _legacy_store(home)
    _with_clio_core_records(legacy)
    host = legacy / "hosts" / "desk"
    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_cte_store(legacy, host, runtime_root=home / ".clio")
    cfg = host / "cte.yaml"

    assert result == CTE_STORE_MIGRATED
    texts = wal_texts((host / "conf/wal/pools.0.bin").read_bytes())
    assert f"path: {host.as_posix()}/storage.bin\n" in texts[0]
    assert f"metadata_log_path: {host.as_posix()}/metadata.log\n" in texts[0]
    assert texts[1] == f"index_log_path: {host.as_posix()}/indexer_index.log"
    (model,) = (host / "conf/models").iterdir()
    assert model.name == f"clio_bdev.{host.as_posix().replace('/', '_')}_storage.bin_node0.0.0.yaml"
    assert f"pool_name: {host.as_posix()}/storage.bin_node0" in model.read_text()
    journal = json.loads((host / "cte-capacity.json").read_text())
    assert journal["config_sha256"] == hashlib.sha256(cfg.read_bytes()).hexdigest()
    # Each path moved once: the new directory lies inside the old one.
    assert "hosts/desk/hosts" not in cfg.read_text()
    assert clio_core_relocate.inspect_store_location(host).matches
    caplog.clear()
    with caplog.at_level(logging.WARNING):  # the start that follows has nothing to do
        assert Path(clio_core_config.default_cte_config_path()) == cfg
    assert "reason=cte_store_relocated" not in caplog.text


def test_a_pool_log_of_unknown_format_is_left_and_the_config_still_moves(
    home: Path,  # noqa: F811 - the fixture
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy = _legacy_store(home)
    _with_clio_core_records(legacy)
    wal = legacy / "conf" / "wal" / "pools.0.bin"
    unknown = wal.read_bytes() + b"\x01"
    wal.write_bytes(unknown)
    host = legacy / "hosts" / "desk"
    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_cte_store(legacy, host, runtime_root=home / ".clio")

    assert result == CTE_STORE_MIGRATED
    assert f"reason={CTE_POOL_LOG_NOT_RELOCATED}" in caplog.text
    assert (host / "conf/wal/pools.0.bin").read_bytes() == unknown
    text = (host / "cte.yaml").read_text()
    assert f'"{host.as_posix()}/storage.bin"' in text and "hosts/desk/hosts" not in text


def test_a_relocation_failure_after_the_move_is_logged_and_finished_at_start(
    home: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    legacy = _legacy_store(home)
    _with_clio_core_records(legacy)
    host = legacy / "hosts" / "desk"
    real = clio_core_relocate.relocate_store

    def interrupted(*_args: object, **_kwargs: object) -> list[str]:
        raise OSError("disk went away")

    monkeypatch.setattr(clio_core_relocate, "relocate_store", interrupted)
    with caplog.at_level(logging.WARNING):
        result = migrate_legacy_cte_store(legacy, host, runtime_root=home / ".clio")
    assert result == CTE_STORE_MIGRATED
    assert f"reason={CTE_STORE_MIGRATION_RELOCATION_INCOMPLETE}" in caplog.text
    assert f'"{legacy.as_posix()}/storage.bin"' in (host / "cte.yaml").read_text()

    monkeypatch.setattr(clio_core_relocate, "relocate_store", real)
    cfg = Path(clio_core_config.default_cte_config_path())  # the next start finishes it

    assert f'"{host.as_posix()}/storage.bin"' in cfg.read_text()
    assert clio_core_relocate.inspect_store_location(host).matches
