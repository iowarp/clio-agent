"""clio-core metadata logs: a torn record is cut out before the daemon starts (FN-14).

Synthetic logs in clio-core 2.4.1's framing (``2LAW``, then ``<u8 type><u64 seq><u32
length>`` and the payload). The shapes are the ones seen live: a whole log (left alone),
a torn tail, a torn record with records appended after it (what the next start leaves),
bytes that are not records with nothing readable after them (refused, unchanged), and a
log in a format this CLIO does not know (left to clio-core).
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc import clio_core_wal_repair as wal
from clio_agent.gact import session_scratch_sweep
from clio_agent.runtime.clio_core_metadata_restore_health import (
    probe_clio_core_metadata_restore,
)
from clio_agent.runtime.status import IntegrationState


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    wal.reset_for_tests()
    yield
    wal.reset_for_tests()


def _record(seq: int, payload: bytes, kind: int = 1) -> bytes:
    return struct.pack("<BQI", kind, seq, len(payload)) + payload


def _payload(seq: int, size: int = 120) -> bytes:
    # Two float -1.0 values, as clio-core's blob records carry them (bf 00 00 80 seen live).
    return (struct.pack("<ff", -1.0, -1.0) + seq.to_bytes(8, "little")) * (size // 16)


def _log(*records: bytes) -> bytes:
    return wal.MAGIC + b"".join(records)


def _config(tmp_path: Path) -> tuple[str, Path]:
    store = tmp_path / "store"
    store.mkdir()
    config = tmp_path / "cte.yaml"
    config.write_text(
        "compose:\n  - mod_name: clio_cte_core\n    performance:\n"
        f'      metadata_log_path: "{(store / "metadata.log").as_posix()}"\n',
        encoding="utf-8",
    )
    return str(config), store


def test_a_whole_log_is_left_alone(tmp_path: Path) -> None:
    config, store = _config(tmp_path)
    data = _log(*(_record(seq, _payload(seq)) for seq in range(10, 20)))
    (store / "metadata.log.blob.0").write_bytes(data)
    (store / "metadata.log.tag.0").write_bytes(wal.MAGIC)

    scans = wal.repair_metadata_logs(config)

    assert [scan.verdict for scan in scans] == ["clean", "clean"]
    assert (store / "metadata.log.blob.0").read_bytes() == data
    assert wal.outcomes() == []


def test_a_torn_tail_is_cut_and_kept_aside(tmp_path: Path) -> None:
    config, store = _config(tmp_path)
    whole = _log(*(_record(seq, _payload(seq)) for seq in range(10, 15)))
    torn = _record(15, _payload(15, 4000))[:900]  # a kill mid-append
    (store / "metadata.log.blob.3").write_bytes(whole + torn)

    (scan,) = wal.repair_metadata_logs(config)

    assert (scan.verdict, scan.start, scan.end) == ("cut", len(whole), len(whole) + 900)
    assert (store / "metadata.log.blob.3").read_bytes() == whole
    (aside,) = store.glob("metadata.log.blob.3.torn-*")
    assert aside.read_bytes() == torn
    assert wal.outcomes()[0]["reason"] == wal.REPAIRED


def test_records_appended_after_a_torn_record_become_readable(tmp_path: Path) -> None:
    """The FN-14 shape: the torn record's length swallows the next start's records.

    **Sabotage:** append without cutting (clio-core 2.4.1) -> the walk by lengths
    lands inside a new record, which this test asserts no longer happens.
    """
    config, store = _config(tmp_path)
    good = _log(*(_record(seq, _payload(seq)) for seq in range(100, 110)))
    torn = _record(110, _payload(110, 1976))[:300]
    appended = b"".join(_record(seq, _payload(seq)) for seq in range(111, 140))
    path = store / "metadata.log.blob.0"
    path.write_bytes(good + torn + appended)
    assert wal.scan_log(path).verdict == "cut"

    (scan,) = wal.repair_metadata_logs(config)

    assert (scan.start, scan.end) == (len(good), len(good) + len(torn))
    assert path.read_bytes() == good + appended
    assert wal.scan_log(path).verdict == "clean"


def test_records_numbered_afresh_after_an_earlier_tear_are_found(tmp_path: Path) -> None:
    """A daemon started on an already-broken log may number from elsewhere."""
    config, store = _config(tmp_path)
    good = _log(*(_record(seq, _payload(seq)) for seq in range(100, 110)))
    torn = _record(110, _payload(110, 1976))[:300]
    appended = b"".join(_record(seq, _payload(seq)) for seq in range(5_000_000_000, 5_000_000_020))
    path = store / "metadata.log.blob.1"
    path.write_bytes(good + torn + appended)

    wal.repair_metadata_logs(config)

    assert path.read_bytes() == good + appended


def test_bytes_that_are_not_records_refuse_typed_and_change_nothing(tmp_path: Path) -> None:
    config, store = _config(tmp_path)
    good = _log(*(_record(seq, _payload(seq)) for seq in range(10, 15)))
    garbage = struct.pack("<BQI", 7, 3, 64) + b"\xff" * 64 + b"\x01" * 77
    path = store / "metadata.log.blob.2"
    path.write_bytes(good + garbage)

    with pytest.raises(wal.ClioCoreMetadataLogUnreadableError) as raised:
        wal.repair_metadata_logs(config)

    assert raised.value.error_type == wal.UNREADABLE
    assert path.read_bytes() == good + garbage
    assert not list(store.glob("*.torn-*"))
    assert wal.restore_suspect() == wal.UNREADABLE


def test_a_log_in_an_unknown_format_is_left_to_clio_core(tmp_path: Path) -> None:
    config, store = _config(tmp_path)
    path = store / "metadata.log.blob.0"
    path.write_bytes(b"WAL3" + b"\x00" * 40)

    (scan,) = wal.repair_metadata_logs(config)

    assert scan.verdict == "unknown_format"
    assert path.read_bytes() == b"WAL3" + b"\x00" * 40
    assert wal.outcomes()[0]["reason"] == wal.UNKNOWN_FORMAT


def test_only_the_configured_logs_are_read(tmp_path: Path) -> None:
    config, store = _config(tmp_path)
    for name in ("metadata.log", "metadata.log.tmp", "metadata.log.blob.0.torn-x", "other.blob.0"):
        (store / name).write_bytes(b"x")
    (store / "metadata.log.blob.0").write_bytes(wal.MAGIC)
    (store / "metadata.log.tag.12").write_bytes(wal.MAGIC)

    names = [path.name for path in wal.metadata_log_files(config)]

    assert names == ["metadata.log.blob.0", "metadata.log.tag.12"]
    assert wal.metadata_log_files(str(tmp_path / "missing.yaml")) == []


def test_the_daemons_restore_failure_is_read_after_the_spawn(tmp_path: Path) -> None:
    config, _store = _config(tmp_path)
    log = tmp_path / "clio-runtime.log"
    log.write_text("an earlier daemon: TransactionLog::Load: old line\n", encoding="utf-8")
    wal.repair_metadata_logs(config, daemon_log=log)
    assert wal.restore_failures() == []
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            "ERROR Load TransactionLog::Load: x.blob.0 has a record at offset 639240 claiming "
            "2147483839 bytes but only 1 remain; ignoring it and everything after\n"
            "WARN CTE restart: metadata restore under /s failed (x); continuing\n"
        )

    assert len(wal.restore_failures()) == 2
    assert wal.restore_suspect() == wal.RESTORE_FAILED


def test_the_scratch_sweep_removes_nothing_when_the_restore_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wal, "restore_suspect", lambda: wal.RESTORE_FAILED)
    monkeypatch.setattr(
        session_scratch_sweep.session_scratch,
        "sweep",
        lambda _activity: pytest.fail("swept session folders against a partial store"),
    )

    assert session_scratch_sweep.sweep_sessions(object()) == []


def test_the_health_row() -> None:
    assert probe_clio_core_metadata_restore(outcomes=[], failures=[], damaged={}) == []
    (repaired,) = probe_clio_core_metadata_restore(
        outcomes=[{"reason": wal.REPAIRED, "path": "x", "start": 1, "end": 2}],
        failures=[],
        damaged={},
    )
    assert repaired.state == IntegrationState.READY
    assert repaired.details["reason"] == wal.REPAIRED
    (failed,) = probe_clio_core_metadata_restore(
        outcomes=[], failures=["TransactionLog::Load"], damaged={}
    )
    assert failed.state == IntegrationState.UNAVAILABLE
    assert failed.details["reason"] == wal.RESTORE_FAILED


def test_a_session_whose_record_does_not_read_back_keeps_its_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen live after a tear: the index lists the session, its record reads back as
    another blob's bytes, so ``get`` answers ``None``. The folder is not "gone"."""

    class _Index:
        def ids(self) -> list[str]:
            return ["sess_damaged"]

    class _Sessions:
        index = _Index()

        def get(self, _sid: str) -> None:
            return None

    seen: dict[str, float | None] = {}

    def sweep(activity: Any) -> list[str]:
        seen.update({sid: activity(sid) for sid in ("sess_damaged", "sess_deleted")})
        return []

    monkeypatch.setattr(session_scratch_sweep.session_scratch, "sweep", sweep)

    session_scratch_sweep.sweep_sessions(_Sessions())

    assert seen == {"sess_damaged": float("inf"), "sess_deleted": None}


def _alloc_entries(*entries: tuple[int, int, int, int, int]) -> bytes:
    return b"".join(struct.pack("<QQQQQ", *entry) for entry in entries)


def _tiered_config(tmp_path: Path) -> tuple[str, Path]:
    config, store = _config(tmp_path)
    with open(config, "a", encoding="utf-8") as handle:
        handle.write(f'    storage:\n      - path: "{(store / "storage.bin").as_posix()}"\n')
    return config, store


def test_a_torn_delete_withholds_the_trailing_frees(tmp_path: Path) -> None:
    from clio_agent.arc import clio_core_alloc_log as alloc

    config, store = _tiered_config(tmp_path)
    good = _log(*(_record(seq, _payload(seq)) for seq in range(10, 15)))
    torn_delete = _record(15, b"\x00" * 31, kind=3)[:20]
    (store / "metadata.log.blob.0").write_bytes(good + torn_delete)
    kept = _alloc_entries((1, 3, 0, 4096, 0), (1, 3, 4096, 4096, 0))
    frees = _alloc_entries((2, 0, 4096, 4096, 0))
    (store / "storage.bin_node0.alloc_log").write_bytes(kept + frees)

    (scan,) = wal.repair_metadata_logs(config)

    assert scan.torn_type == 3
    assert (store / "storage.bin_node0.alloc_log").read_bytes() == kept
    (aside,) = store.glob("storage.bin_node0.alloc_log.withheld-*")
    assert aside.read_bytes() == frees
    assert any(item["reason"] == alloc.WITHHELD for item in wal.outcomes())


def test_the_allocation_log_is_left_alone_unless_a_put_or_delete_was_torn(
    tmp_path: Path,
) -> None:
    from clio_agent.arc import clio_core_alloc_log as alloc

    config, store = _tiered_config(tmp_path)
    good = _log(*(_record(seq, _payload(seq)) for seq in range(10, 15)))
    (store / "metadata.log.blob.0").write_bytes(good + _record(15, b"x" * 40, kind=0)[:20])
    entries = _alloc_entries((1, 3, 0, 4096, 0), (2, 0, 0, 4096, 0))
    (store / "storage.bin_node0.alloc_log").write_bytes(entries)

    wal.repair_metadata_logs(config)  # a torn create (type 0) logs no frees
    assert (store / "storage.bin_node0.alloc_log").read_bytes() == entries

    odd = entries + b"\x01\x02"  # not whole entries: reported, untouched
    (store / "storage.bin_node0.alloc_log").write_bytes(odd)
    assert alloc.withhold_trailing_frees(config, "t")[0]["reason"] == alloc.UNVERIFIED
    assert (store / "storage.bin_node0.alloc_log").read_bytes() == odd
    (store / "storage.bin_node0.alloc_log").write_bytes(_alloc_entries((1, 3, 0, 4096, 0)))
    assert alloc.withhold_trailing_frees(config, "t")[0]["reason"] == alloc.UNCHANGED
