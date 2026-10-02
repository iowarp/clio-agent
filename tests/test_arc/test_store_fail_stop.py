"""clio-core is THE context store: an init failure is a typed error, never another store.

Supersedes the #897 loud degrade to LocalFS (owner principle of the agent-loop rebuild,
2026-09-28: the only fallback is DSPy ``History`` when the platform cannot run clio-core
at all). Whatever fails -- capacity preflight, daemon spawn, attach, version refusal --
the store factory raises :class:`ArcStoreUnavailableError` carrying the typed reason, and
nothing runs on local files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.arc import clio_core_config, clio_core_file_capacity, storage
from clio_agent.arc.init_degradation import ArcStoreUnavailableError
from clio_agent.errors import ClioError


class _CapacityRefused(RuntimeError):
    degradation_reason = "clio_core_file_capacity_unavailable"


def test_a_clio_core_init_failure_is_a_typed_error_not_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise _CapacityRefused("file tier needs 50 GiB, 35 GiB free")

    monkeypatch.setattr(clio_core_file_capacity, "preflight_clio_core_config", refuse)

    data_dir = tmp_path / "arc"
    data_dir.mkdir()
    config = tmp_path / "c.yaml"
    config.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=9413,
            conf_dir="C:/x/conf",
            file_tier="C:/x/storage.bin",
            file_capacity="50GB",
            ram_budget="1GB",
            metadata_log="C:/x/metadata.log",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ArcStoreUnavailableError) as caught:
        storage.make_arc_store(backend="cte", data_dir=data_dir, config_path=str(config))

    assert isinstance(caught.value, ClioError)
    assert caught.value.reason == "clio_core_file_capacity_unavailable"
    assert "50 GiB" in str(caught.value)
    written = sorted(str(p.relative_to(data_dir)) for p in data_dir.rglob("*"))
    assert written == [], f"nothing may be written to local files: {written}"


def test_there_is_no_local_files_store_to_choose() -> None:
    assert not hasattr(storage, "LocalFSStore")
    with pytest.raises(ValueError, match="clio-core"):
        storage.make_arc_store(backend="local")


def test_a_missing_clio_core_config_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ArcStoreUnavailableError):
        storage.make_arc_store(backend="cte", config_path=str(tmp_path / "absent.yaml"))
