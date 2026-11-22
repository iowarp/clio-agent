"""The opt-in metadata-only shared-memory cache in CLIO's seed (iowarp-core 2.5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from clio_agent import conf
from clio_agent.arc import clio_core_config, clio_core_shm_cache
from tests.test_arc.test_clio_core_config import _store


@pytest.fixture(autouse=True)
def _fresh_mode() -> Iterator[None]:
    clio_core_shm_cache.mode.cache_clear()
    yield
    clio_core_shm_cache.mode.cache_clear()


def _seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]) -> Path:
    monkeypatch.setattr(conf, "_STORE", _store(env=env, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    clio_core_shm_cache.mode.cache_clear()
    return Path(clio_core_config.default_cte_config_path())


def _chain(path: Path) -> dict[str, dict]:
    return {m["mod_name"]: m for m in yaml.safe_load(path.read_text())["compose"]}


def test_off_by_default_the_seed_has_no_cache(monkeypatch, tmp_path) -> None:
    modules = _chain(_seed(monkeypatch, tmp_path, {}))
    assert "clio_cte_core_shm_cache_writethru" not in modules
    assert modules["clio_cte_indexer"]["next_pool_id"] == "512.0"


def test_md_only_composes_the_cache_between_indexer_and_core(monkeypatch, tmp_path) -> None:
    """SABOTAGE: leave the indexer's next pool at the core and puts bypass the cache."""
    path = _seed(monkeypatch, tmp_path, {"CLIO_ARC_CTE_SHM_CACHE": "md_only"})
    modules = _chain(path)
    cache = modules["clio_cte_core_shm_cache_writethru"]
    assert cache["md_only"] is True  # never the data mode (acks before the WAL)
    assert (cache["pool_id"], cache["next_pool_id"], cache["entry_pool_id"]) == (
        "568.0",
        "512.0",
        "564.0",
    )
    assert modules["clio_cte_indexer"]["next_pool_id"] == "568.0"
    names = [m["mod_name"] for m in yaml.safe_load(path.read_text())["compose"]]
    assert names.index("clio_cte_core_shm_cache_writethru") < names.index("clio_cte_indexer")


def test_turning_it_off_restores_the_original_seed(monkeypatch, tmp_path) -> None:
    plain = _seed(monkeypatch, tmp_path, {}).read_text()
    _seed(monkeypatch, tmp_path, {"CLIO_ARC_CTE_SHM_CACHE": "md_only"})
    restored = _seed(monkeypatch, tmp_path, {"CLIO_ARC_CTE_SHM_CACHE": "off"}).read_text()
    assert restored == plain


def test_applying_twice_changes_nothing(monkeypatch, tmp_path) -> None:
    env = {"CLIO_ARC_CTE_SHM_CACHE": "md_only"}
    once = _seed(monkeypatch, tmp_path, env).read_text()
    assert _seed(monkeypatch, tmp_path, env).read_text() == once


def test_a_hand_written_indexer_entry_is_left_alone(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CTE_SHM_CACHE", "md_only")
    path = tmp_path / "cte.yaml"
    text = 'compose:\n  - mod_name: clio_cte_indexer\n    next_pool_id: "600.0"\n'
    path.write_text(text)
    clio_core_shm_cache.apply_shm_cache(path)
    assert path.read_text() == text


def test_an_unknown_mode_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CTE_SHM_CACHE", "data")
    with pytest.raises(ValueError, match="shm_cache"):
        clio_core_shm_cache.mode()
