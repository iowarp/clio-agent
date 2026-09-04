"""Disk-bounded lifecycle tests for the session-private CTE fixture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._cte_isolation import CteIsolation, isolate_cte_env, remove_private_cte_root


def test_private_cte_tier_is_bounded_and_removed_after_session(tmp_path: Path) -> None:
    root = tmp_path / "clio-agent-cte-123-unit0"
    root.mkdir()
    environment: dict[str, str] = {}

    isolation = isolate_cte_env(root, environment)

    assert environment["CLIO_RUNTIME_STATE_DIR"] == str(isolation.state_dir)
    assert environment["CLIO_ARC_STORE_CONFIG"] == str(isolation.config_path)
    config = isolation.config_path.read_text(encoding="utf-8")
    assert 'path: "' + (root / "store/storage.bin").as_posix() + '"' in config
    assert 'capacity_limit: "512MB"' in config

    remove_private_cte_root(isolation.root, retry_delay_seconds=0)

    assert not root.exists()


def test_live_session_private_cte_tier_does_not_preallocate_gigabytes(
    _clio_private_cte_daemon: CteIsolation | None,
) -> None:
    isolation = _clio_private_cte_daemon
    if isolation is None:
        return

    runtime = Path(os.environ["CLIO_TEST_RUNTIME_DIR"]).resolve()
    assert isolation.root.resolve().is_relative_to(runtime / "cte")
    assert isolation.file_tier_path.is_file()
    assert isolation.file_tier_path.stat().st_size <= 512 * 1024**2


def test_private_cte_cleanup_refuses_an_unowned_directory_name(tmp_path: Path) -> None:
    unrelated = tmp_path / "other-data"
    unrelated.mkdir()
    marker = unrelated / "keep.txt"
    marker.write_text("operator data", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected CTE test root"):
        remove_private_cte_root(unrelated)

    assert marker.read_text(encoding="utf-8") == "operator data"
