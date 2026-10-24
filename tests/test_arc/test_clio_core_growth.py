"""Capacity recovery preserves data, explicit caps and stable target identities."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from clio_agent.arc import clio_core_growth as growth
from clio_agent.arc.clio_core_config import _DEFAULT_CTE_CONFIG_TEMPLATE, _cte_yaml_path


class Future:
    def __init__(self, result: Any = None, code: int = 0) -> None:
        self.value = result
        self.code = code

    def done(self) -> bool:
        return True

    def wait(self, timeout: float = 0) -> int:
        return self.code

    def result(self, timeout: float = 0) -> Any:
        return self.value


class Native:
    def __init__(self, config: Path) -> None:
        self.config = config
        self.old_size = 0
        self.free = 0
        self.register_code = 0
        self.registered: list[tuple[str, int, Any]] = []
        self.refreshed = 0

    def AsyncGetBlobSize(self, *args: Any) -> Future:  # noqa: N802
        return Future(self.old_size, 0 if self.old_size else 1)

    def AsyncStatTargets(self, *args: Any) -> Future:  # noqa: N802
        self.refreshed += 1
        return Future(code=0)

    def AsyncGetCapacity(self, *args: Any) -> Future:  # noqa: N802
        assert self.refreshed > 0
        return Future(SimpleNamespace(total_capacity=8 << 20, remaining_capacity=self.free))

    def AsyncGetTargetInfo(self, *args: Any) -> Future:  # noqa: N802
        return Future(SimpleNamespace(remaining_space=self.free))

    def AsyncRegisterTarget(
        self, path: str, media: Any, size: int, query: Any, pool: Any
    ) -> Future:  # noqa: N802
        tiers = yaml.safe_load(self.config.read_text())["compose"][1]["storage"]
        assert str(tiers[-1]["path"]) + "_node0" == path
        self.registered.append((path, size, pool))
        if self.register_code == 0:
            self.free = size
        return Future(code=self.register_code)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(growth, "_default_cte_dir", lambda: tmp_path)
    monkeypatch.delenv("CLIO_ARC_CTE_FILE_CAPACITY", raising=False)
    monkeypatch.setenv("CLIO_ARC_CTE_AUTO_EXPAND", "true")
    config = tmp_path / "cte.yaml"
    config.write_text(
        _DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=31413,
            conf_dir=_cte_yaml_path(tmp_path / "conf"),
            ram_budget="1GB",
            file_tier=_cte_yaml_path(tmp_path / "storage.bin"),
            file_capacity="8MB",
            metadata_log=_cte_yaml_path(tmp_path / "metadata.log"),
            indexer="",
        ),
        encoding="utf-8",
    )
    growth.initialize_managed_capacity(config)
    cte = SimpleNamespace(
        PoolQuery=SimpleNamespace(Local=lambda: "local"),
        PoolId=lambda major, minor: (major, minor),
        BdevType=SimpleNamespace(kFile="file"),
    )
    monkeypatch.setattr(growth.shutil, "disk_usage", lambda path: SimpleNamespace(free=20 << 30))
    return SimpleNamespace(_config_path=str(config), _client=Native(config), _cte=cte)


def test_full_store_adds_stable_target_and_preserves_original(store: Any) -> None:
    config = Path(store._config_path)
    original = yaml.safe_load(config.read_text())["compose"][1]["storage"][0]
    backing = config.parent / "storage.bin_node0"
    backing.write_bytes(b"old committed bytes")
    assert growth.recover_capacity(store, "tag", "record", 1024)
    tiers = yaml.safe_load(config.read_text())["compose"][1]["storage"]
    assert tiers[0] == original and len(tiers) == 2
    assert backing.read_bytes() == b"old committed bytes"
    assert store._client.registered[0][2] == (513, 1)
    assert not growth.recover_capacity(store, "tag", "next", 1024)
    assert len(store._client.registered) == 1


def test_zero_growth_overwrite_does_not_expand(store: Any) -> None:
    store._client.old_size = 1024
    assert not growth.recover_capacity(store, "tag", "record", 100)
    assert store._client.refreshed == 0
    assert not store._client.registered


def test_space_available_is_not_misdiagnosed_as_full(store: Any) -> None:
    store._client.free = 1024
    assert not growth.recover_capacity(store, "tag", "record", 1024)
    assert not store._client.registered


@pytest.mark.parametrize(
    "setting,value", [("CLIO_ARC_CTE_FILE_CAPACITY", "8MB"), ("CLIO_ARC_CTE_AUTO_EXPAND", "false")]
)
def test_explicit_limits_are_respected(
    store: Any, monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    monkeypatch.setenv(setting, value)
    before = Path(store._config_path).read_bytes()
    assert not growth.recover_capacity(store, "tag", "record", 1024)
    assert Path(store._config_path).read_bytes() == before
    assert not store._client.registered


def test_manual_edit_disables_growth(store: Any) -> None:
    config = Path(store._config_path)
    config.write_text(config.read_text().replace("8MB", "16MB"))
    assert not growth.recover_capacity(store, "tag", "record", 1024)
    assert not store._client.registered


def test_custom_config_is_not_enrolled(store: Any) -> None:
    Path(store._config_path).with_name("cte-capacity.json").unlink()
    assert not growth.recover_capacity(store, "tag", "record", 1024)


def test_old_unchanged_default_seed_can_recover(store: Any) -> None:
    config = Path(store._config_path)
    config.with_name("cte-capacity.json").unlink()
    config.write_text(config.read_text().replace("8MB", "2GB"))
    assert growth.recover_capacity(store, "tag", "record", 1024)


def test_disk_reserve_is_preserved(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(growth.shutil, "disk_usage", lambda path: SimpleNamespace(free=1 << 30))
    before = Path(store._config_path).read_bytes()
    with pytest.raises(RuntimeError, match="insufficient free space"):
        growth.recover_capacity(store, "tag", "record", 1024)
    assert Path(store._config_path).read_bytes() == before
    assert not store._client.registered


def test_config_failure_resumes_same_plan_before_registration(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Path(store._config_path)
    original = growth._atomic_text

    def fail_config(path: Path, text: str) -> None:
        if path == config:
            raise OSError("disk write refused")
        original(path, text)

    monkeypatch.setattr(growth, "_atomic_text", fail_config)
    with pytest.raises(OSError, match="disk write refused"):
        growth.recover_capacity(store, "tag", "record", 1024)
    assert not store._client.registered
    assert len(yaml.safe_load(config.read_text())["compose"][1]["storage"]) == 1
    monkeypatch.setattr(growth, "_atomic_text", original)
    assert growth.recover_capacity(store, "tag", "record", 1024)
    assert len(yaml.safe_load(config.read_text())["compose"][1]["storage"]) == 2


def test_registration_refusal_preserves_pending_target_identity(store: Any) -> None:
    store._client.register_code = 1
    with pytest.raises(RuntimeError, match="registration refused"):
        growth.recover_capacity(store, "tag", "record", 1024)
    store._client.register_code = 0
    assert growth.recover_capacity(store, "tag", "record", 1024)
    assert [row[2] for row in store._client.registered] == [(513, 1), (513, 1)]
    assert len(yaml.safe_load(Path(store._config_path).read_text())["compose"][1]["storage"]) == 2


def test_concurrent_refusals_add_one_target(store: Any) -> None:
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda index: growth.recover_capacity(store, "tag", str(index), 1024), range(4)
            )
        )
    assert results.count(True) == 1
    assert len(store._client.registered) == 1


def _pre_durability_seed(config: Path) -> str:
    """The seed as CLIO wrote it before the durable file tier, enrolled for growth."""
    from clio_agent.arc.clio_core_durability import _PERSISTENCE_LINE

    text = config.read_text()
    assert _PERSISTENCE_LINE in text
    old = text.replace(_PERSISTENCE_LINE, "", 1)
    config.write_text(old)
    growth.initialize_managed_capacity(config)
    return old


def test_clios_own_durability_upgrade_keeps_growth(store: Any) -> None:
    """Live (#1846): the in-place upgrade changed the config CLIO had enrolled, growth
    then took it for a person's edit, and the full store stopped the server booting."""
    from clio_agent.arc.clio_core_durability import ensure_seeded_config_durable

    config = Path(store._config_path)
    _pre_durability_seed(config)
    ensure_seeded_config_durable(config)
    assert growth.recover_capacity(store, "tag", "record", 1024)


def test_a_config_upgraded_before_this_fix_recovers(store: Any) -> None:
    config = Path(store._config_path)
    old = _pre_durability_seed(config)
    from clio_agent.arc.clio_core_durability import _PERSISTENCE_LINE

    config.write_text(
        old.replace("        score: 1.0\n", "        score: 1.0\n" + _PERSISTENCE_LINE, 1)
    )
    assert growth.recover_capacity(store, "tag", "record", 1024)


def test_a_person_s_edit_after_the_upgrade_still_opts_out(store: Any) -> None:
    from clio_agent.arc.clio_core_durability import ensure_seeded_config_durable

    config = Path(store._config_path)
    _pre_durability_seed(config)
    ensure_seeded_config_durable(config)
    config.write_text(config.read_text().replace("8MB", "16MB"))
    assert not growth.recover_capacity(store, "tag", "record", 1024)
    assert not store._client.registered
