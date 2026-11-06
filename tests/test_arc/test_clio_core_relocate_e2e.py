"""A moved data folder's clio-core store, end to end on the installed iowarp-core.

A real daemon writes a blob into a store laid out as CLIO seeds it; the folder is
moved to a longer path, an empty directory is left at the old path (anything clio-core
opened there would appear in it), and the next start relocates the store and reads the
blob back through the real ``ClioCoreStore``.

The second test shows that clio-core reads the rewritten pool log itself: a pool the
config does not compose is re-created from its pool-log record, and it is re-created at
the relocated path. Sabotage (recorded): relocating without rewriting the pool log
(``rewrite_pool_log=False``) re-creates the indexer at the OLD path, and dropping the
:func:`ensure_store_location` call from ``default_cte_config_path`` starts an empty
store at the old path; each turns these tests red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from clio_agent import path_relocation
from clio_agent.arc import clio_core_relocate
from clio_agent.arc.clio_core_indexer import indexer_block
from clio_agent.arc.clio_core_relocate import ensure_store_location, inspect_store_location
from tests._cte_isolation import (
    _PRIVATE_CONFIG_TEMPLATE,
    PRIVATE_MAIN_SEGMENT,
    PRIVATE_METADATA_SEGMENT,
    PRIVATE_QUEUE_DEPTH,
    PRIVATE_RAM_BDEV,
    cte_isolation_available,
    reap_private_daemon,
    reserve_port_block,
)

pytestmark = [
    pytest.mark.timeout(400),  # three daemon starts and clean stops, ~20 s each
    pytest.mark.skipif(not cte_isolation_available(), reason="needs the clio-core launcher"),
]

_REPO = Path(__file__).resolve().parents[2]
_CLIENT = Path(__file__).with_name("_clio_core_relocate_client.py")
_BLOB_BYTES = 10_000  # _clio_core_relocate_client.BLOB


def _seed(cte_dir: Path) -> None:
    """``cte.yaml`` laid out as CLIO seeds it (every location under ``cte_dir``)."""
    cte_dir.mkdir(parents=True)
    here = cte_dir.as_posix()
    (cte_dir / "cte.yaml").write_text(
        _PRIVATE_CONFIG_TEMPLATE.format(
            port=reserve_port_block(),
            conf_dir=f"{here}/conf",
            file_tier=f"{here}/storage.bin",
            metadata_log=f"{here}/metadata.log",
            file_tier_capacity="32MB",
            queue_depth=PRIVATE_QUEUE_DEPTH,
            main_segment=PRIVATE_MAIN_SEGMENT,
            metadata_segment=PRIVATE_METADATA_SEGMENT,
            ram_bdev=PRIVATE_RAM_BDEV,
            indexer=indexer_block(f"{here}/indexer_index.log"),
        ),
        encoding="utf-8",
    )


def _env(tmp_path: Path, *, cte_dir: Path | None = None, config: Path | None = None) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CLIO_USER_DIR", "CLIO_ARC_STORE_CONFIG", "CLIO_SERVER_CONF"}
        and not key.startswith("CLIO_AGENT_")
    }
    env.update(
        PYTHONPATH=os.pathsep.join([str(_REPO / "src"), str(_REPO), env.get("PYTHONPATH", "")]),
        CLIO_AGENT_HOME=str(tmp_path / "clio-home"),
        CLIO_RUNTIME_STATE_DIR=str(tmp_path / "runtime"),
        CLIO_ARC_STORE="cte",
        CTP_LOG_LEVEL="error",
        # Its own shared-memory namespace (#1148): never the suite daemon's.
        USER=f"relocate-e2e-{uuid.uuid4().hex}",
    )
    if cte_dir is not None:
        env["CLIO_ARC_CTE_DIR"] = str(cte_dir)
    if config is not None:
        env["CLIO_ARC_STORE_CONFIG"] = str(config)
    return env


def _run(phase: str, env: dict) -> dict:
    try:
        done = subprocess.run(  # noqa: S603 - this interpreter, an in-repo client
            [sys.executable, "-B", str(_CLIENT), phase],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        reap_private_daemon(Path(env["CLIO_RUNTIME_STATE_DIR"]))
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert done.returncode == 0 and lines, f"rc={done.returncode}\n{done.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT ") :])


def _move(tmp_path: Path) -> tuple[Path, Path]:
    """Write the blob in ``old``, move the folder to ``new`` and leave ``old`` empty."""
    old = tmp_path / "home" / "data" / "cte"
    _seed(old)
    written = _run("write", _env(tmp_path, cte_dir=old))
    assert written["store_type"] == "ClioCoreStore" and written["identical"], written
    assert any((old / "conf" / "wal").glob("pools.*.bin")), "clio-core wrote no pool log"
    new = tmp_path / "a-longer-place-after-the-move" / "home" / "data" / "cte"
    new.parent.mkdir(parents=True)
    shutil.move(old, new)
    old.mkdir()  # whatever clio-core opened at the old path would appear here
    return old, new


def _names(root: Path, needle: bytes) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*"))
        if (path.is_file() and needle in path.read_bytes()) or needle.decode() in path.name
    ]


def test_a_moved_store_starts_where_it_is_and_reads_its_blob_back(tmp_path: Path) -> None:
    old, new = _move(tmp_path)

    moved = _run("read", _env(tmp_path, cte_dir=new))

    assert moved["store_type"] == "ClioCoreStore", moved
    assert moved["read"] == _BLOB_BYTES and moved["identical"], moved
    relocation = moved["relocation"]
    assert relocation["from"] == old.as_posix() and relocation["to"] == str(new)
    assert {"cte.yaml", "wal/pools.0.bin"} <= set(relocation["files"])
    assert list(old.iterdir()) == [], "clio-core used the old location"
    assert _names(new, old.as_posix().encode()) == []
    assert _names(new, old.as_posix().replace("/", "_").encode()) == []

    again = _run("read", _env(tmp_path, cte_dir=new))  # in place now: starts as is

    assert again["read"] == _BLOB_BYTES and again["identical"] and again["relocation"] is None
    assert list(old.iterdir()) == []


def test_a_pool_the_config_does_not_compose_is_replayed_at_its_relocated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = _move(tmp_path)
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    # This process's relocation records (served by /v1/health) stay this test's.
    monkeypatch.setattr(path_relocation, "_records", [])
    monkeypatch.setattr(clio_core_relocate, "_last", None)
    assert ensure_store_location(new) is not None
    assert inspect_store_location(new).matches
    config = new / "cte.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[: text.index("  - mod_name: clio_cte_indexer")], encoding="utf-8")
    for index_log in new.glob("indexer_index.log*"):
        index_log.unlink()

    replayed = _run("read", _env(tmp_path, config=config))

    assert replayed["read"] == _BLOB_BYTES and replayed["identical"], replayed
    assert (new / "indexer_index.log").is_file(), "the pool-log indexer was not re-created"
    assert list(old.iterdir()) == [], "clio-core re-created a pool at the old path"
