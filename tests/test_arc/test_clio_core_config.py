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
from clio_agent.arc import clio_core_config

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
    # The regression this test guards: the ram tier is NEVER generated as "0g".
    assert _ram_tier_cap(text) == "1GB"
    assert '"0g"' not in text.split("cte_ram_tier")[1].split("score")[0]


def test_generator_respects_env_ram_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(
        conf, "_STORE", _store(env={"CLIO_ARC_CTE_RAM_CAPACITY": "4GB"}, tmp_path=tmp_path)
    )
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")

    clio_core_config.default_cte_config_path()
    text = (tmp_path / "cte" / "cte.yaml").read_text(encoding="utf-8")
    assert _ram_tier_cap(text) == "4GB"


def test_generator_never_rewrites_existing_user_file(monkeypatch, tmp_path):
    """An existing cte.yaml (even a stale 0g one) is left byte-for-byte untouched."""
    monkeypatch.setattr(conf, "_STORE", _store(env={}, tmp_path=tmp_path))
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    cte_dir = tmp_path / "cte"
    cte_dir.mkdir(parents=True)
    stale = clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
        conf_dir="c", file_tier="f", file_capacity="1GB", ram_capacity="0g", metadata_log="m"
    )
    (cte_dir / "cte.yaml").write_text(stale, encoding="utf-8")

    clio_core_config.default_cte_config_path()
    # Not regenerated: the user's explicit (even if unbounded) value survives.
    assert (cte_dir / "cte.yaml").read_text(encoding="utf-8") == stale
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
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
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
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
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
    assert result.cap == "1GB"
    assert result.source == "generator-default"
