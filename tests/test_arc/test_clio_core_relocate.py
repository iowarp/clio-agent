"""A moved CLIO data folder keeps using its own clio-core store (clio_core_relocate).

Each test builds a store as CLIO seeds it and clio-core leaves it (``cte.yaml``, the
pool log in its binary record layout, a file bdev model file named after its path,
the capacity journal), moves it, and checks the start-time relocation or refusal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import socket
import struct
from pathlib import Path

import pytest

from clio_agent import path_relocation
from clio_agent.arc import (
    clio_core_attach,
    clio_core_config,
    clio_core_daemon_version,
    clio_core_relocate,
)
from clio_agent.arc.clio_core_indexer import indexer_block
from clio_agent.arc.clio_core_relocate import (
    CTE_POOL_LOG_NOT_RELOCATED,
    CTE_POOL_LOG_RELOCATION_DEFERRED,
    CTE_STORE_LOCATION_ELSEWHERE,
    CTE_STORE_LOCATION_IN_USE,
    CTE_STORE_LOCATION_MISMATCH,
    CteStoreLocationError,
    ensure_store_location,
    inspect_store_location,
    wal_records,
    wal_texts,
)
from clio_agent.arc.clio_core_threads import core_threads
from clio_agent.arc.init_degradation import ArcStoreUnavailableError
from clio_agent.runtime.cte_store_location import probe_cte_store_location


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _u64(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<Q", len(data)) + data


def _record(major: int, pool: str, chimod: str, params: bytes) -> bytes:
    out = b"\x01\x01" + struct.pack("<Q", major)
    for field in (pool.encode(), chimod.encode(), params):
        out += struct.pack("<I", len(field)) + field
    return out


def _wal(storage: str, metadata: str, index: str) -> bytes:
    core = f"mod_name: clio_cte_core\nstorage:\n  - path: {storage}\nperformance:\n"
    core += f"  metadata_log_path: {metadata}\n  transaction_log_capacity: 32MB"
    return (
        _record(301, "ram::chi_default_bdev", "clio_bdev", _u64("mod_name: clio_bdev"))
        + _record(512, "cte_main", "clio_cte_core", _u64("cte_main") + b"\x00\x02\x00" + _u64(core))
        + _record(564, "clio_cte_indexer", "clio_cte_indexer", _u64(f"index_log_path: {index}"))
    )


def _seed(root: Path, *, port: int, storage: str | None = None) -> Path:
    """A store as CLIO seeds it at ``root`` and clio-core leaves it after running."""
    here = root.as_posix()
    tier = storage or f"{here}/storage.bin"
    (root / "conf" / "wal").mkdir(parents=True)
    (root / "conf" / "models").mkdir()
    text = clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
        core_port=port,
        core_threads=core_threads(),
        conf_dir=f"{here}/conf",
        file_tier=tier,
        file_capacity="2GB",
        ram_budget="1GB",
        metadata_log=f"{here}/metadata.log",
        indexer=indexer_block(f"{here}/indexer_index.log"),
    )
    (root / "cte.yaml").write_text(text, encoding="utf-8")
    (root / "conf" / "wal" / "pools.0.bin").write_bytes(
        _wal(tier, f"{here}/metadata.log", f"{here}/indexer_index.log")
    )
    model = root / "conf" / "models" / f"clio_bdev.{tier.replace('/', '_')}_node0.0.0.yaml"
    model.write_text(f"chimod_name: clio_bdev\npool_name: {tier}_node0\n", encoding="utf-8")
    (root / "conf" / "models" / "clio_cte_core.cte_main.0.0.yaml").write_text("pool_name: x\n")
    (root / "metadata.log.blob.0").write_bytes(b"\x00\x01records")
    digest = hashlib.sha256(text.encode()).hexdigest()
    (root / "cte-capacity.json").write_text(json.dumps({"schema": 1, "config_sha256": digest}))
    return root


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(clio_core_relocate, "_last", None)
    monkeypatch.setattr(path_relocation, "_records", [])


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _moved_store(tmp_path: Path) -> tuple[Path, Path]:
    old = _seed(tmp_path / "old" / "cte", port=_free_port())
    new = tmp_path / "a-longer-new-place" / "data" / "cte"
    new.parent.mkdir(parents=True)
    shutil.move(old, new)
    return old, new


def test_a_moved_store_is_relocated_and_the_old_path_is_not_touched(tmp_path: Path) -> None:
    old, new = _moved_store(tmp_path)
    _seed(old, port=_free_port())  # a decoy store at the old path
    decoy = _tree(old)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    clio_core_daemon_version.record_daemon_version(
        runtime, daemon_binary="clio_run", config_path=str(old / "cte.yaml")
    )

    record = ensure_store_location(new)

    assert record is not None and record["reason"] == "cte_store_relocated"
    assert record["from"] == old.as_posix() and record["to"] == str(new)
    assert _tree(old) == decoy
    for path in new.rglob("*"):
        assert clio_core_relocate.mangled(old.as_posix()) + "_" not in path.name
        if path.is_file():
            assert old.as_posix().encode() not in path.read_bytes()
    wal = (new / "conf/wal/pools.0.bin").read_bytes()
    assert [fields[0] for _head, fields in wal_records(wal)] == [
        b"ram::chi_default_bdev",
        b"cte_main",
        b"clio_cte_indexer",
    ]
    assert wal == _wal(
        *(f"{new.as_posix()}/{n}" for n in ("storage.bin", "metadata.log", "indexer_index.log"))
    )
    model = (
        new
        / "conf/models"
        / f"clio_bdev.{new.as_posix().replace('/', '_')}_storage.bin_node0.0.0.yaml"
    )
    assert (
        model.read_text()
        == f"chimod_name: clio_bdev\npool_name: {new.as_posix()}/storage.bin_node0\n"
    )
    journal = json.loads((new / "cte-capacity.json").read_text())
    assert journal["config_sha256"] == hashlib.sha256((new / "cte.yaml").read_bytes()).hexdigest()
    assert clio_core_relocate.last_relocation() == record
    stopped = clio_core_daemon_version.read_daemon_version(runtime)
    assert stopped is not None and stopped.config_path == str(new / "cte.yaml")
    assert ensure_store_location(new) is None  # done: the next start has nothing to do


def test_a_store_in_place_is_left_byte_for_byte(tmp_path: Path) -> None:
    store = _seed(tmp_path / "cte", port=_free_port())
    before = _tree(store)

    assert ensure_store_location(store) is None
    assert inspect_store_location(store).matches
    assert _tree(store) == before


def test_a_custom_path_outside_the_old_store_is_left_alone(tmp_path: Path) -> None:
    disk = (tmp_path / "fast-disk" / "storage.bin").as_posix()
    old = _seed(tmp_path / "old" / "cte", port=_free_port(), storage=disk)
    new = tmp_path / "new" / "cte"
    new.parent.mkdir()
    shutil.move(old, new)

    ensure_store_location(new)

    config = (new / "cte.yaml").read_text()
    assert f'path: "{disk}"' in config
    assert f'conf_dir: "{new.as_posix()}/conf"' in config
    assert any(disk in text for text in wal_texts((new / "conf/wal/pools.0.bin").read_bytes()))


def test_an_old_location_in_use_is_refused(tmp_path: Path) -> None:
    old, new = _moved_store(tmp_path)
    _seed(old, port=_free_port())  # the other install, still running on its store
    before = _tree(new)

    with (old / "metadata.log.blob.0").open("rb"):
        with pytest.raises(CteStoreLocationError) as refused:
            ensure_store_location(new)

    assert refused.value.degradation_reason == CTE_STORE_LOCATION_IN_USE
    assert "Fix: stop every CLIO" in str(refused.value)
    assert _tree(new) == before


def test_a_served_clio_core_port_is_refused(tmp_path: Path) -> None:
    _old, new = _moved_store(tmp_path)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        text = (new / "cte.yaml").read_text()
        (new / "cte.yaml").write_text(text.replace("port: ", f"port: {port}  #", 1))
        with pytest.raises(CteStoreLocationError, match=f"clio-core port {port}"):
            ensure_store_location(new)


def test_a_config_naming_a_store_elsewhere_that_holds_it_is_used_there(tmp_path: Path) -> None:
    """Only the config is here; the store it names is in place there (never refused)."""
    other = _seed(tmp_path / "other" / "cte", port=_free_port())
    here = tmp_path / "empty" / "cte"
    here.mkdir(parents=True)
    shutil.copy(other / "cte.yaml", here / "cte.yaml")
    before, theirs = _tree(here), _tree(other)

    location = inspect_store_location(here)

    assert location.matches and location.kept_at == other.as_posix()
    assert ensure_store_location(here) is None
    assert _tree(here) == before and _tree(other) == theirs


def test_a_pool_log_naming_a_tier_the_config_no_longer_names_is_left(tmp_path: Path) -> None:
    """A person moved the file tier in cte.yaml; the pool log names the old tier until
    clio-core composes the pool from cte.yaml. The store is in place: nothing changes."""
    store = _seed(tmp_path / "cte", port=_free_port())
    here = store.as_posix()
    elsewhere = (tmp_path / "elsewhere" / "data.bin").as_posix()
    (store / "conf/wal/pools.0.bin").write_bytes(
        _wal(elsewhere, f"{here}/metadata.log", f"{here}/indexer_index.log")
    )
    before = _tree(store)

    assert inspect_store_location(store).matches
    assert ensure_store_location(store) is None
    assert _tree(store) == before


def test_a_pool_log_left_behind_by_the_host_move_is_relocated(tmp_path: Path) -> None:
    """The pre-host-key store moved into ``<cte>/hosts/<host>`` had only its config fixed."""
    legacy = tmp_path / "data" / "cte"
    host = legacy / "hosts" / "desk"
    _seed(host, port=_free_port())
    (host / "conf/wal/pools.0.bin").write_bytes(
        _wal(
            *(
                f"{legacy.as_posix()}/{n}"
                for n in ("storage.bin", "metadata.log", "indexer_index.log")
            )
        )
    )

    record = ensure_store_location(host)

    assert record is not None and record["from"] == legacy.as_posix()
    assert (host / "conf/wal/pools.0.bin").read_bytes() == _wal(
        *(f"{host.as_posix()}/{n}" for n in ("storage.bin", "metadata.log", "indexer_index.log"))
    )
    assert record["files"] == ["wal/pools.0.bin"]  # the config already named this directory


def test_an_interrupted_relocation_is_finished_by_the_next_start(tmp_path: Path) -> None:
    old, new = _moved_store(tmp_path)
    transform = clio_core_relocate._relocator(old.as_posix(), new.as_posix())
    wal = new / "conf/wal/pools.0.bin"
    wal.write_bytes(
        clio_core_relocate.rewrite_wal(wal.read_bytes(), old.as_posix().encode(), transform)
    )

    record = ensure_store_location(new)

    assert record is not None and "cte.yaml" in record["files"]
    assert inspect_store_location(new).matches


def test_doctor_reports_the_mismatch_then_the_relocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _moved_store(tmp_path)
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: new)
    env = {"CLIO_ARC_STORE_CONFIG": ""}

    (pending,) = probe_cte_store_location(env=env)
    assert pending.state.value == "degraded"
    assert pending.details["reason"] == "cte_store_relocation_pending"
    assert pending.details["recorded"] == old.as_posix()

    ensure_store_location(new)
    (done,) = probe_cte_store_location(env=env)
    assert done.state.value == "ready"
    assert done.details["reason"] == "cte_store_relocated"
    assert done.details["from"] == old.as_posix()


def test_a_refusal_reaches_the_attach_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _old, new = _moved_store(tmp_path)
    wal = new / "conf/wal/pools.0.bin"
    wal.write_bytes(wal.read_bytes()[:-3])  # truncated: not the known layout
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: new)
    clio_core_attach.reset_attach_state()

    with pytest.raises(ArcStoreUnavailableError) as failure:
        clio_core_attach.seed_default_config()

    state = clio_core_attach.attach_state_snapshot()
    assert failure.value.reason == CTE_STORE_LOCATION_MISMATCH
    assert state.phase.value == "unavailable" and state.reason == CTE_STORE_LOCATION_MISMATCH
    clio_core_attach.reset_attach_state()


# ---- never refused in place; every refusal typed ----------------------------------------


def _host_moved_store(tmp_path: Path) -> tuple[Path, Path]:
    """A store the host-key migration moved into ``<cte>/hosts/desk``: its config names
    the new directory, its pool log and model still name the old ``<cte>``."""
    legacy = tmp_path / "data" / "cte"
    host = _seed(legacy / "hosts" / "desk", port=_free_port())
    (host / "conf/wal/pools.0.bin").write_bytes(
        _wal(
            *(
                f"{legacy.as_posix()}/{n}"
                for n in ("storage.bin", "metadata.log", "indexer_index.log")
            )
        )
    )
    return legacy, host


def test_an_in_place_store_with_an_unreadable_pool_log_starts(tmp_path: Path) -> None:
    store = _seed(tmp_path / "cte", port=_free_port())
    (store / "conf/wal/pools.0.bin").write_bytes(b"\x07 not a pool log path: /nowhere/x")
    before = _tree(store)

    assert ensure_store_location(store) is None
    assert _tree(store) == before


def test_a_store_kept_in_a_subdirectory_of_its_own_directory_is_in_place(tmp_path: Path) -> None:
    """``<dir>/cte.yaml`` naming ``<dir>/store/{conf,storage.bin,...}``, with an old
    metadata log left beside it: the subdirectory is this store's own, never a move."""
    root = tmp_path / "cte"
    store = _seed(root / "store", port=_free_port())
    shutil.move(store / "cte.yaml", root / "cte.yaml")
    (root / "metadata.log.blob.0").write_bytes(b"left over")
    before = _tree(root)

    assert inspect_store_location(root).matches
    assert ensure_store_location(root) is None
    assert _tree(root) == before


def test_data_a_person_put_on_another_disk_is_in_place_even_with_leftovers_here(
    tmp_path: Path,
) -> None:
    """cte.yaml names ``/disk/storage.bin`` and ``/disk/metadata.log`` (a seed-shaped
    directory) and keeps its conf here; old copies of the files are still here."""
    store = _seed(tmp_path / "cte", port=_free_port())
    disk = (tmp_path / "disk").as_posix()
    text = (store / "cte.yaml").read_text()
    for name in ("storage.bin", "metadata.log"):
        text = text.replace(f"{store.as_posix()}/{name}", f"{disk}/{name}")
    (store / "cte.yaml").write_text(text)
    (store / "storage.bin_node0").write_bytes(b"leftover")
    before = _tree(store)

    assert ensure_store_location(store) is None
    assert _tree(store) == before


def test_a_moved_store_whose_pool_log_cannot_be_rewritten_is_refused_unchanged(
    tmp_path: Path,
) -> None:
    old, new = _moved_store(tmp_path)
    wal = new / "conf/wal/pools.0.bin"
    wal.write_bytes(wal.read_bytes() + b"\x01")  # a trailing partial record
    before = _tree(new)

    with pytest.raises(CteStoreLocationError) as refused:
        ensure_store_location(new)

    assert refused.value.degradation_reason == CTE_STORE_LOCATION_MISMATCH
    assert "pool log cannot be rewritten" in str(refused.value)
    assert f"Fix: restore the data folder to {old.as_posix()}" in str(refused.value)
    assert _tree(new) == before


def test_a_relocation_that_leaves_the_old_place_named_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _moved_store(tmp_path)
    monkeypatch.setattr(clio_core_relocate, "relocate_store", lambda *_a, **_k: [])

    with pytest.raises(CteStoreLocationError) as refused:
        ensure_store_location(new)

    assert refused.value.degradation_reason == CTE_STORE_LOCATION_MISMATCH
    assert f"left cte.yaml, conf/wal/pools.0.bin naming {old.as_posix()}" in str(refused.value)


def test_a_host_moved_pool_log_in_use_is_deferred_not_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _legacy, host = _host_moved_store(tmp_path)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        text = (host / "cte.yaml").read_text()
        (host / "cte.yaml").write_text(text.replace("port: ", f"port: {port}  #", 1))
        before = _tree(host)
        with caplog.at_level(logging.WARNING):
            assert ensure_store_location(host) is None

    assert f"reason={CTE_POOL_LOG_RELOCATION_DEFERRED}" in caplog.text
    assert _tree(host) == before


def test_a_host_moved_pool_log_of_unknown_format_is_left_and_the_store_starts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _legacy, host = _host_moved_store(tmp_path)
    wal = host / "conf/wal/pools.0.bin"
    wal.write_bytes(wal.read_bytes() + b"\x01\x01")
    before = _tree(host)

    with caplog.at_level(logging.WARNING):
        assert ensure_store_location(host) is None

    assert f"reason={CTE_POOL_LOG_NOT_RELOCATED}" in caplog.text
    assert _tree(host) == before


def test_a_model_clio_core_already_wrote_for_the_new_path_is_kept(tmp_path: Path) -> None:
    old, new = _moved_store(tmp_path)
    models = new / "conf/models"
    current = models / f"clio_bdev.{mangled_tier(new)}_node0.0.0.yaml"
    current.write_text(f"pool_name: {new.as_posix()}/storage.bin_node0\nlearned: 2\n")

    record = ensure_store_location(new)

    assert record is not None
    assert current.read_text().endswith("learned: 2\n")
    assert sorted(path.name for path in models.glob("clio_bdev.*")) == [current.name]


def mangled_tier(root: Path) -> str:
    return f"{root.as_posix()}/storage.bin".replace("/", "_")


def test_doctor_reports_a_store_used_where_its_config_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _seed(tmp_path / "other" / "cte", port=_free_port())
    here = tmp_path / "empty" / "cte"
    here.mkdir(parents=True)
    shutil.copy(other / "cte.yaml", here / "cte.yaml")
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: here)

    (row,) = probe_cte_store_location(env={"CLIO_ARC_STORE_CONFIG": ""})

    assert row.state.value == "ready"
    assert row.details["reason"] == CTE_STORE_LOCATION_ELSEWHERE
    assert row.details["recorded"] == other.as_posix()
