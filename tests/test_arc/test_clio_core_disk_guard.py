"""No clio-core write while the metadata log's disk is nearly full (FN-14).

iowarp-core 2.4.1 tears its metadata log when that file system fills, answers the put
as a success and logs nothing (reproduced with the log on a 3 MB tmpfs). Every write and
delete CLIO sends therefore checks the free space first and refuses, typed, below the
reserve; the refusal is a lost write like any other, so the person sees why.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc import clio_core_async_ops as ops
from clio_agent.arc import clio_core_disk_guard as guard
from clio_agent.arc import clio_core_retry

_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    guard.metadata_log_dir.cache_clear()
    clio_core_retry._reset_put_write_health_for_tests()
    yield
    guard.metadata_log_dir.cache_clear()
    clio_core_retry._reset_put_write_health_for_tests()


def _store(tmp_path: Path) -> Any:
    config = tmp_path / "cte.yaml"
    config.write_text(
        f'    performance:\n      metadata_log_path: "{(tmp_path / "metadata.log").as_posix()}"\n',
        encoding="utf-8",
    )

    class _Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def AsyncPutBlob(self, *_a: Any) -> Any:  # noqa: N802 - native shape
            self.calls.append("put")
            raise AssertionError("a put reached clio-core below the reserve")

        def AsyncDelBlob(self, *_a: Any) -> Any:  # noqa: N802 - native shape
            self.calls.append("delete")
            raise AssertionError("a delete reached clio-core below the reserve")

    class _Store:
        _config_path = str(config)
        _client = _Client()
        _tag_ids = {"segments": 5}

    return _Store()


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda _p: _Usage(10**12, 0, free))


def test_the_metadata_log_directory_comes_from_the_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert guard.metadata_log_dir(store._config_path) == str(tmp_path)
    assert guard.metadata_log_dir(str(tmp_path / "missing.yaml")) == ""


def test_a_put_below_the_reserve_never_reaches_clio_core_and_is_a_typed_lost_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Sabotage:** drop ``ensure_room`` from ``AsyncPutTag.PutBlob`` -> the put reaches
    the fake client, which fails the test."""
    store = _store(tmp_path)
    _free(monkeypatch, 10 * 1024 * 1024)
    monkeypatch.setenv("CLIO_ARC_CTE_METADATA_DISK_RESERVE", "256MB")
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S", "0")

    with pytest.raises(guard.ClioCoreDiskLowRefused) as raised:
        ops.store_put(store, "segments", "row/x", b"body")

    assert raised.value.degradation_reason == guard.DISK_LOW
    assert store._client.calls == []
    lost = clio_core_retry.last_lost_put_write()
    assert lost is not None and guard.DISK_LOW in lost.error


def test_a_batch_and_deletes_below_the_reserve_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _free(monkeypatch, 1024)
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S", "0")

    lost = ops.store_put_many(store, "segments", [("a", b"1"), ("b", b"2")])

    assert set(lost) == {"a", "b"}
    assert all(isinstance(error, guard.ClioCoreDiskLowRefused) for error in lost.values())
    for delete in (
        lambda: ops.store_delete(store, "segments", "a"),
        lambda: ops.store_delete_together(store, "segments", ["a"]),
        lambda: ops.store_delete_many(store, "segments", ["a"]),
    ):
        with pytest.raises(guard.ClioCoreDiskLowRefused):
            delete()
    assert store._client.calls == []


def test_enough_room_or_a_zero_reserve_lets_writes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _free(monkeypatch, 10 * 1024**3)
    guard.ensure_room(store, "x")
    _free(monkeypatch, 0)
    monkeypatch.setenv("CLIO_ARC_CTE_METADATA_DISK_RESERVE", "0")
    guard.ensure_room(store, "x")


def test_the_person_reads_that_the_disk_is_nearly_full(tmp_path: Path) -> None:
    from clio_agent.gact import core_write_refusal

    refusal = guard.ClioCoreDiskLowRefused("row/x", str(tmp_path), 1024, 4096)
    clio_core_retry._record_lost_write(name="row/x", payload=b"x", exc=refusal)
    core_write_refusal._TRACED.clear()

    info = core_write_refusal._refusal("/v1/sessions/sess_a/messages")

    assert info is not None
    assert info.message == core_write_refusal.MESSAGE_DISK_LOW
    assert info.details["cause"] == guard.DISK_LOW


pytest.importorskip("clio_cte_core_ext")


@pytest.mark.timeout(400)  # two private daemon starts and stops
def test_real_clio_core_writes_nothing_below_the_reserve_and_resumes_after(
    tmp_path: Path,
) -> None:
    """Real clio-core on a private daemon: with the reserve above the free space, a put is
    refused typed and the metadata log gains no record; with the default reserve it lands."""
    from clio_agent.arc import clio_core_config  # noqa: PLC0415
    from tests.test_arc.test_clio_core_offload_spill import (  # noqa: PLC0415
        _reap_private_daemon,
        _require_launcher,
        _reserve_port_block,
    )

    _require_launcher()
    home = tmp_path / "home"
    (home / ".clio").mkdir(parents=True)
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (tmp_path / "conf").mkdir()
    port = _reserve_port_block()
    config = tmp_path / "cte.yaml"
    config.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=port,
            core_threads=2,
            conf_dir=(tmp_path / "conf").as_posix(),
            ram_budget="256MB",
            file_tier=(store_dir / "storage.bin").as_posix(),
            file_capacity="32MB",
            metadata_log=(store_dir / "metadata.log").as_posix(),
            indexer="",
        ),
        encoding="utf-8",
    )
    child = (
        "import sys\n"
        "from clio_agent.arc import storage\n"
        "store = storage.make_arc_store()\n"
        "try:\n"
        "    store.put('segments', 'probe', b'x' * 5000)\n"
        "    print('WROTE')\n"
        "except Exception as exc:\n"
        "    print('REFUSED', getattr(exc, 'degradation_reason', ''), type(exc).__name__)\n"
        "storage.release_runtime_client()\n"
    )

    def run(reserve: str) -> str:
        env = {
            **os.environ,
            "HOME": str(home),
            "CLIO_RUNTIME_STATE_DIR": str(home / ".clio"),
            "CLIO_ARC_STORE": "cte",
            "CLIO_ARC_STORE_CONFIG": str(config),
            "CLIO_CORE_PORT": str(port),
            "CLIO_ARC_CTE_METADATA_DISK_RESERVE": reserve,
            "CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S": "0",
            "CTP_LOG_LEVEL": "error",
        }
        try:
            done = subprocess.run(  # noqa: S603 - fixed interpreter
                [sys.executable, "-c", child],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=Path(__file__).parents[2],
            )
        finally:
            time.sleep(0.5)
            _reap_private_daemon(home)
        return done.stdout + done.stderr

    def blob_log_bytes() -> int:
        return sum(path.stat().st_size for path in store_dir.glob("metadata.log.blob.?"))

    huge = f"{(1 << 50) // (1024 * 1024)}MB"  # far more than any disk has free
    refused = run(huge)
    assert "REFUSED clio_core_metadata_disk_low ClioCoreDiskLowRefused" in refused, refused
    assert blob_log_bytes() == 4 * len(list(store_dir.glob("metadata.log.blob.?")))
    wrote = run("1MB")
    assert "WROTE" in wrote, wrote
    assert blob_log_bytes() > 4 * len(list(store_dir.glob("metadata.log.blob.?")))
