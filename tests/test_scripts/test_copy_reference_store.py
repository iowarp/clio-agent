"""Tests for ``scripts/copy_reference_store.py``: an independent copy of a reference store.

The pool WAL embeds the store's absolute paths behind length fields; these tests build a
WAL in that record layout, rewrite it to a path of another length and parse it back, and
copy a small store tree end to end (text files, the WAL, a model file named after its
path, the runtime client registry, the port).
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scripts.copy_reference_store import (
    CopyRefused,
    _wal_records,
    copy_store,
    rewrite_wal,
    verify,
)


def _u64_text(text: bytes) -> bytes:
    return struct.pack("<Q", len(text)) + text


def _record(major: int, pool: bytes, chimod: bytes, params: bytes) -> bytes:
    out = b"\x01\x01" + struct.pack("<Q", major)
    for field in (pool, chimod, params):
        out += struct.pack("<I", len(field)) + field
    return out


def _wal(root: bytes) -> bytes:
    yaml = b"storage:\n  - path: " + root + b"/storage.bin\nmetadata_log_path: " + root + b"/m.log"
    core = _u64_text(b"clio_cte_core") + _u64_text(b"cte_main") + b"\x00\x02\x00" + _u64_text(yaml)
    index = _u64_text(b"clio_cte_indexer") + _u64_text(b"index_log_path: " + root + b"/i.log")
    return (
        _record(301, b"ram::chi_default_bdev", b"clio_bdev", _u64_text(b"clio_bdev"))
        + _record(512, b"cte_main", b"clio_cte_core", core)
        + _record(564, b"clio_cte_indexer", b"clio_cte_indexer", index)
    )


@pytest.mark.parametrize("new", [b"/x/short", b"/a/much/longer/destination/path/than/before"])
def test_rewrite_wal_fixes_every_length_field(new: bytes) -> None:
    src = b"/data/refstore/home"
    rewritten = rewrite_wal(_wal(src), src, new)

    assert src not in rewritten
    assert rewritten == _wal(new)  # byte-identical to a WAL written for the new path
    assert [fields[0] for _head, fields in _wal_records(rewritten, "wal")] == [
        b"ram::chi_default_bdev",
        b"cte_main",
        b"clio_cte_indexer",
    ]


def test_rewrite_wal_refuses_a_path_outside_a_text_string() -> None:
    src = b"/data/refstore"
    params = b"\xff" * 3 + src  # no length field encloses it
    wal = _record(512, b"cte_main", b"clio_cte_core", params)

    with pytest.raises(CopyRefused, match="not inside a length-prefixed"):
        rewrite_wal(wal, src, b"/other")


def test_wal_records_refuses_a_truncated_log() -> None:
    with pytest.raises(CopyRefused, match="overruns"):
        _wal_records(_wal(b"/data/refstore")[:-5], "wal")


def _store(root: Path) -> None:
    vm = root / "home/data/cte/hosts/vm"
    (vm / "conf/wal").mkdir(parents=True)
    (vm / "conf/models").mkdir(parents=True)
    path = str(root).encode()
    (vm / "cte.yaml").write_bytes(b"networking:\n  port: 28567\nstorage: " + path + b"/s.bin\n")
    (vm / "conf/wal/pools.0.bin").write_bytes(_wal(path + b"/home"))
    mangled = str(root).replace("/", "_") + "_home_s.bin_node0"
    (vm / f"conf/models/clio_bdev.{mangled}.0.0.yaml").write_bytes(b"pool_name: " + path)
    (vm / "storage.bin_node0").write_bytes(b"\x00" * 64 + b"opaque")
    (root / "runtime/clio-runtime.clients").mkdir(parents=True)
    (root / "runtime/clio-runtime.clients/1234").write_text("")
    (root / "env.sh").write_bytes(
        b"export CLIO_AGENT_HOME=" + path + b"/home\nexport CLIO_CORE_PORT=28567\n"
    )


def test_copy_store_leaves_nothing_naming_the_source(tmp_path: Path) -> None:
    src, dst = tmp_path / "refstore", tmp_path / "copies" / "a-longer-copy-name"
    _store(src)

    changed = copy_store(src, dst, port=29071, replace=False)

    assert verify(dst, str(src).encode()) == []
    assert any(line.startswith("wal ") for line in changed)
    assert any(line.startswith("rename ") for line in changed)
    vm = dst / "home/data/cte/hosts/vm"
    assert b"  port: 29071\n" in (vm / "cte.yaml").read_bytes()
    assert b"export CLIO_CORE_PORT=29071\n" in (dst / "env.sh").read_bytes()
    assert not list((dst / "runtime/clio-runtime.clients").iterdir())
    _wal_records((vm / "conf/wal/pools.0.bin").read_bytes(), "wal")
    assert verify(src, str(src).encode())  # the source is untouched


def test_copy_store_refuses_an_unknown_binary_of_another_length(tmp_path: Path) -> None:
    src, dst = tmp_path / "refstore", tmp_path / "copy-of-another-length"
    _store(src)
    (src / "home/blob.bin").write_bytes(b"\x00\x01" + str(src).encode())

    with pytest.raises(CopyRefused, match="binary file of unknown format"):
        copy_store(src, dst, port=None, replace=False)
    assert not dst.exists()  # a refused copy is removed


def test_copy_store_refuses_an_existing_destination(tmp_path: Path) -> None:
    src, dst = tmp_path / "refstore", tmp_path / "copy"
    _store(src)
    dst.mkdir()

    with pytest.raises(CopyRefused, match="--replace"):
        copy_store(src, dst, port=None, replace=False)
