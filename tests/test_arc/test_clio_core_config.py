"""Tests for the clio-core CTE config generator + ram hot-tier capacity policy (#890).

The generated ram tier ``capacity_limit`` must default to a bounded value (never the
``0g`` = 80%-of-DRAM footgun), be configurable via ``arc.cte.ram_capacity`` / env
``CLIO_ARC_CTE_RAM_CAPACITY``, fail loud on a malformed value, and never rewrite an
existing user config file. Assertions read the real generated ``cte.yaml``.
"""

from __future__ import annotations

import pytest
import yaml

from clio_agent import conf
from clio_agent.arc import clio_core_config, clio_core_indexer

# ---- capacity parser (fail-loud validation) -------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0g", 0),
        ("2GB", 2 * 1024**3),
        ("512MB", 512 * 1024**2),
        ("128mb", 128 * 1024**2),
        ("1g", 1024**3),
        ("2048", 2048),
        ("1.5GB", int(1.5 * 1024**3)),
    ],
)
def test_parse_capacity_bytes_valid(value, expected):
    assert clio_core_config.parse_capacity_bytes(value) == expected


@pytest.mark.parametrize("value", ["2gigs!", "", "GB", "two", "2 3 GB", "-2GB", "2GBz"])
def test_parse_capacity_bytes_rejects_garbage(value):
    with pytest.raises(ValueError, match="invalid CTE capacity"):
        clio_core_config.parse_capacity_bytes(value)


# ---- default ram-cap resolution -------------------------------------------- #


def _store(env: dict | None = None, file_yaml: str | None = None, tmp_path=None):
    """Build a ConfigStore isolated to injected env + an optional workspace config."""
    cwd = (tmp_path / "cwd") if tmp_path is not None else None
    if file_yaml is not None and tmp_path is not None:
        cfg = cwd / ".clio" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(file_yaml, encoding="utf-8")
    return conf.ConfigStore(
        home=(tmp_path / "home") if tmp_path is not None else None,
        cwd=cwd,
        env=env or {},
    )


def test_default_ram_capacity_is_bounded_2gb(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    assert clio_core_config._default_cte_ram_capacity() == "1GB"


def test_default_ram_capacity_env_override(monkeypatch, tmp_path):
    monkeypatch.setattr(
        conf, "_STORE", _store(env={"CLIO_ARC_CTE_RAM_CAPACITY": "512MB"}, tmp_path=tmp_path)
    )
    assert clio_core_config._default_cte_ram_capacity() == "512MB"


def test_default_ram_capacity_file_config_wins_over_env(monkeypatch, tmp_path):
    store = _store(
        env={"CLIO_ARC_CTE_RAM_CAPACITY": "512MB"},
        file_yaml="arc:\n  cte:\n    ram_capacity: 4GB\n",
        tmp_path=tmp_path,
    )
    monkeypatch.setattr(conf, "_STORE", store)
    assert clio_core_config._default_cte_ram_capacity() == "4GB"


def test_default_ram_capacity_invalid_value_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(
        conf, "_STORE", _store(env={"CLIO_ARC_CTE_RAM_CAPACITY": "2gigs!"}, tmp_path=tmp_path)
    )
    with pytest.raises(ValueError, match="invalid CTE capacity"):
        clio_core_config._default_cte_ram_capacity()


# ---- generator writes a bounded cap into the real file --------------------- #


# A LEGACY tier-present config shape (pre-#906 generated files still in the
# wild): used to test the READ path for ram-data-tier configs.
_LEGACY_TIER_TEMPLATE = """runtime:
  conf_dir: "{conf_dir}"
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_query: local
    pool_id: "301.0"
    bdev_type: ram
    capacity: "0g"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    storage:
      - path: "ram::cte_ram_tier"
        bdev_type: "ram"
        capacity_limit: "{ram_capacity}"
        score: 1.0
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "{file_capacity}"
        score: 0.0
    performance:
      metadata_log_path: "{metadata_log}"
"""


def _ram_tier_cap(cte_yaml_text: str) -> str:
    data = yaml.safe_load(cte_yaml_text)
    for module in data["compose"]:
        for tier in module.get("storage", []) or []:
            if str(tier.get("path", "")).endswith("cte_ram_tier"):
                return str(tier["capacity_limit"])
    raise AssertionError("no cte_ram_tier in generated config")


def test_generator_writes_bounded_ram_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")

    path = clio_core_config.default_cte_config_path()
    text = (tmp_path / "cte" / "cte.yaml").read_text(encoding="utf-8")
    assert path == str(tmp_path / "cte" / "cte.yaml")
    # #906 disk-only shape: NO ram data tier; the arena = the budget (hard
    # bound); the regression guarded here is any unbounded RAM footprint.
    assert "cte_ram_tier" not in text
    assert 'capacity: "1GB"' in text.split("clio_bdev")[1].split("clio_cte_core")[0]
    assert '"0g"' not in text


def test_generator_respects_env_ram_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(
        conf, "_STORE", _store(env={"CLIO_ARC_CTE_RAM_CAPACITY": "4GB"}, tmp_path=tmp_path)
    )
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")

    clio_core_config.default_cte_config_path()
    text = (tmp_path / "cte" / "cte.yaml").read_text(encoding="utf-8")
    # budget 4GB -> arena capacity 4GB; still disk-only data
    assert "cte_ram_tier" not in text
    assert 'capacity: "4GB"' in text.split("clio_bdev")[1].split("clio_cte_core")[0]


def test_generator_never_rewrites_existing_user_file(monkeypatch, tmp_path):
    """An existing cte.yaml keeps every user value (even a stale 0g cap); the only changes
    are the durability line, without which clio-core keeps nothing across a restart,
    and the indexer entry appended to compose, without which nothing can be searched."""
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    cte_dir = tmp_path / "cte"
    cte_dir.mkdir(parents=True)
    stale = _LEGACY_TIER_TEMPLATE.format(
        conf_dir="c", file_tier="f", file_capacity="1GB", ram_capacity="0g", metadata_log="m"
    )
    (cte_dir / "cte.yaml").write_text(stale, encoding="utf-8")

    clio_core_config.default_cte_config_path()
    # Not regenerated: the user's explicit (even if unbounded) value survives.
    upgraded = (cte_dir / "cte.yaml").read_text(encoding="utf-8")
    assert upgraded.replace('        persistence_level: "temporary"\n', "").startswith(stale)
    assert upgraded.endswith(clio_core_indexer.indexer_block(str(cte_dir / "indexer_index.log")))
    assert _ram_tier_cap((cte_dir / "cte.yaml").read_text(encoding="utf-8")) == "0g"


def test_generator_fails_loud_on_invalid_env_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(
        conf, "_STORE", _store(env={"CLIO_ARC_CTE_RAM_CAPACITY": "not-a-size"}, tmp_path=tmp_path)
    )
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    with pytest.raises(ValueError, match="invalid CTE capacity"):
        clio_core_config.default_cte_config_path()


# ---- effective_ram_cap (doctor read-only resolution) ----------------------- #


def test_effective_ram_cap_reads_existing_file(tmp_path):
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(
        _LEGACY_TIER_TEMPLATE.format(
            conf_dir="c", file_tier="f", file_capacity="50GB", ram_capacity="2GB", metadata_log="m"
        ),
        encoding="utf-8",
    )
    result = clio_core_config.effective_ram_cap(env={"CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert result.file_exists is True
    assert result.cap == "2GB"
    assert result.unbounded is False
    assert result.parse_error is None


def test_effective_ram_cap_flags_0g(tmp_path):
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(
        _LEGACY_TIER_TEMPLATE.format(
            conf_dir="c", file_tier="f", file_capacity="1GB", ram_capacity="0g", metadata_log="m"
        ),
        encoding="utf-8",
    )
    result = clio_core_config.effective_ram_cap(env={"CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert result.cap == "0g"
    assert result.unbounded is True


def test_effective_ram_cap_default_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    result = clio_core_config.effective_ram_cap(env={})
    assert result.file_exists is False
    assert result.cap is None  # disk-only default: no ram data tier (#906)
    assert result.bdev_capacity == "1GB"  # arena = the budget (hard bound)
    assert result.source == "generator-default"


# ---- the BM25 indexer chimod (clio-core#905) -------------------------------- #


def test_generated_default_config_composes_the_scoped_indexer(monkeypatch, tmp_path):
    """The seeded topology composes the indexer after the core, scoped and logged."""
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")

    clio_core_config.default_cte_config_path()
    data = yaml.safe_load((tmp_path / "cte" / "cte.yaml").read_text(encoding="utf-8"))
    names = [m.get("mod_name") for m in data["compose"]]
    assert names[-2:] == ["clio_cte_core", "clio_cte_indexer"]
    indexer = data["compose"][-1]
    assert indexer["pool_id"] == clio_core_indexer.INDEXER_POOL_ID
    assert indexer["next_pool_id"] == clio_core_indexer.CORE_POOL_ID
    assert indexer["tag_re"] == clio_core_indexer.INDEX_TAG_RE
    assert indexer["blob_re"] == clio_core_indexer.INDEX_BLOB_RE
    assert indexer["index_log_path"].endswith("indexer_index.log")


def test_an_older_seeded_config_gains_the_indexer_once(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    clio_core_config.default_cte_config_path()
    cfg = tmp_path / "cte" / "cte.yaml"
    seeded = cfg.read_text(encoding="utf-8")
    cfg.write_text(seeded[: seeded.index("  - mod_name: clio_cte_indexer")], encoding="utf-8")

    with caplog.at_level("WARNING", logger="clio_agent.arc.clio_core_indexer"):
        clio_core_config.default_cte_config_path()
        clio_core_config.default_cte_config_path()
    assert cfg.read_text(encoding="utf-8") == seeded
    assert caplog.text.count("upgraded in place") == 1


def test_bind_client_to_indexer_sets_the_pool_or_reports_its_absence(tmp_path, caplog):
    with_indexer = tmp_path / "with.yaml"
    with_indexer.write_text(
        "compose:\n  - mod_name: clio_cte_core\n    pool_id: '512.0'\n"
        + clio_core_indexer.indexer_block("idx.log"),
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    assert clio_core_indexer.bind_client_to_indexer(str(with_indexer), env) == "564.0"
    assert env == {"CLIO_CTE_POOL": "564.0", "CLIO_CTE_POOL_BOUND_BY": "clio-agent"}

    explicit = {"CLIO_CTE_POOL": "563.0"}  # an operator's cache chain top is kept
    assert clio_core_indexer.bind_client_to_indexer(str(with_indexer), explicit) == "563.0"

    bare = tmp_path / "bare.yaml"
    bare.write_text("compose:\n  - mod_name: clio_cte_core\n    pool_id: '512.0'\n")
    none: dict[str, str] = {}
    with caplog.at_level("WARNING", logger="clio_agent.arc.clio_core_indexer"):
        assert clio_core_indexer.bind_client_to_indexer(str(bare), none) == ""
    assert none == {}
    assert clio_core_indexer.CLIO_CORE_SEARCH_UNAVAILABLE in caplog.text


def test_a_pool_clio_bound_is_recomputed_for_a_config_without_the_indexer(tmp_path):
    """A later attach to a bare config must not keep routing to a pool it lacks (hang)."""
    with_indexer = tmp_path / "with.yaml"
    with_indexer.write_text(
        "compose:\n  - mod_name: clio_cte_core\n    pool_id: '512.0'\n"
        + clio_core_indexer.indexer_block("idx.log"),
        encoding="utf-8",
    )
    bare = tmp_path / "bare.yaml"
    bare.write_text("compose:\n  - mod_name: clio_cte_core\n    pool_id: '512.0'\n")
    env: dict[str, str] = {}
    assert clio_core_indexer.bind_client_to_indexer(str(with_indexer), env) == "564.0"
    child = dict(env)  # a child process inherits the parent's binding and its marker
    assert clio_core_indexer.bind_client_to_indexer(str(bare), child) == ""
    assert "CLIO_CTE_POOL" not in child
