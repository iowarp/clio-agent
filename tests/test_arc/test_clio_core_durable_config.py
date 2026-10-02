"""clio-core holds the agent's context across restarts, so its data tier must be durable.

A clio-core tier left at the default ``volatile`` persistence level keeps nothing across a
daemon restart (verified against iowarp-core 2.2.1: a record written, the daemon stopped
cleanly, a new daemon on the same config -> the record is gone; with the file tier at
``persistence_level: "temporary"`` it is read back). So the seeded config is durable, the
seeded config of an existing install is upgraded in place, and a user-supplied config
that is not durable is a typed error rather than a store that forgets on restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_durability import (
    config_is_durable,
    ensure_seeded_config_durable,
    require_durable_config,
)
from clio_agent.arc.init_degradation import ArcStoreUnavailableError

_VOLATILE_SEED = clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.replace(
    '        persistence_level: "temporary"\n', ""
).format(
    core_port=9413,
    conf_dir="C:/x/conf",
    file_tier="C:/x/storage.bin",
    file_capacity="50GB",
    ram_budget="1GB",
    metadata_log="C:/x/metadata.log",
)


def test_the_seeded_config_keeps_data_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path)

    cfg = Path(clio_core_config.default_cte_config_path())

    assert config_is_durable(cfg.read_text(encoding="utf-8"))


def test_an_existing_seeded_config_is_made_durable_in_place(tmp_path: Path) -> None:
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(_VOLATILE_SEED, encoding="utf-8")
    assert not config_is_durable(_VOLATILE_SEED)

    ensure_seeded_config_durable(cfg)

    text = cfg.read_text(encoding="utf-8")
    assert config_is_durable(text)
    assert text.replace('        persistence_level: "temporary"\n', "") == _VOLATILE_SEED


def test_a_user_config_that_forgets_on_restart_is_a_typed_error(tmp_path: Path) -> None:
    cfg = tmp_path / "mine.yaml"
    cfg.write_text(_VOLATILE_SEED, encoding="utf-8")

    with pytest.raises(ArcStoreUnavailableError) as caught:
        require_durable_config(str(cfg))

    assert caught.value.reason == "clio_core_not_durable"
    assert 'persistence_level: "temporary"' in str(caught.value)
