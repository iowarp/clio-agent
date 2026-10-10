"""28a: provenance paths saved on one cluster node follow the deployment to the next.

Managed deployments live in ``…/services/<host>/<name>`` (shared home, one
directory per host). A saved Flowcept settings path or attention capture
directory written on node A must resolve to node B's copy once B installed it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.attention import files
from clio_agent.gact.infrastructure import service_paths
from clio_agent.gact.infrastructure.service_paths import this_host_counterpart
from clio_agent.gact.provenance import factory
from clio_agent.gact.provenance.flowcept import FlowceptProviderConfig


def _settings(root: Path, host: str) -> Path:
    path = root / "services" / host / "flowcept" / "settings.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


@pytest.fixture
def on_gpub099(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_paths.platform, "node", lambda: "gpub099.delta.example")


@pytest.mark.usefixtures("on_gpub099")
def test_other_hosts_path_resolves_to_this_hosts_copy(tmp_path: Path) -> None:
    old = _settings(tmp_path, "gpub081")
    new = _settings(tmp_path, "gpub099")
    assert this_host_counterpart(str(old)) == str(new)


@pytest.mark.usefixtures("on_gpub099")
def test_path_is_kept_until_this_host_has_installed_it(tmp_path: Path) -> None:
    old = _settings(tmp_path, "gpub081")
    assert this_host_counterpart(str(old)) == str(old)


@pytest.mark.usefixtures("on_gpub099")
def test_this_hosts_and_unmanaged_paths_are_unchanged(tmp_path: Path) -> None:
    own = _settings(tmp_path, "gpub099")
    assert this_host_counterpart(str(own)) == str(own)
    plain = tmp_path / "flowcept" / "settings.yaml"
    assert this_host_counterpart(str(plain)) == str(plain)
    assert this_host_counterpart("") == ""
    # A "services" directory too shallow to be <services>/<host>/<name> is not managed.
    assert this_host_counterpart(str(tmp_path / "services" / "x")) == str(
        tmp_path / "services" / "x"
    )


@pytest.mark.usefixtures("on_gpub099")
def test_attention_files_dir_follows_the_vllm_deployment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = tmp_path / "services" / "gpub081" / "clio-vllm" / "evidence"
    new = tmp_path / "services" / "gpub099" / "clio-vllm" / "evidence"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    monkeypatch.setenv("CLIO_PROVENANCE_ATTENTION_FILES_DIR", str(old))
    assert files.configured_files_dir() == str(new)


@pytest.mark.usefixtures("on_gpub099")
def test_flowcept_installed_after_boot_attaches_with_this_hosts_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLIO boots before the morning launcher installs Flowcept on the new node."""
    import clio_agent.gact.provenance.flowcept as flowcept_module

    old = _settings(tmp_path, "gpub081")
    seen: list[str] = []

    class _Recording:
        durable = False
        queryable = True
        flush_durable = False
        flush_note = ""

        def __init__(self, config: FlowceptProviderConfig) -> None:
            seen.append(config.settings_path)
            if not Path(config.settings_path).is_file():
                raise FileNotFoundError("Flowcept settings file not found")

        def close(self) -> None:
            return None

    monkeypatch.setattr(flowcept_module, "FlowceptProvenanceProvider", _Recording)
    monkeypatch.setenv("FLOWCEPT_SETTINGS_PATH", str(old))
    provider: Any = factory._deferred_flowcept(factory._flowcept_config())
    assert not provider.attached
    # Never the previous node's settings: its services listen on that node's loopback.
    new_path = tmp_path / "services" / "gpub099" / "flowcept" / "settings.yaml"
    assert seen == [str(new_path)]
    assert str(old) not in seen

    new = _settings(tmp_path, "gpub099")
    provider._next_attempt = 0.0
    assert provider.recheck()
    assert seen[-1] == str(new)


@pytest.mark.usefixtures("on_gpub099")
def test_live_service_settings_never_resolve_to_another_hosts_copy(tmp_path: Path) -> None:
    old = _settings(tmp_path, "gpub081")
    expected = tmp_path / "services" / "gpub099" / "flowcept" / "settings.yaml"
    assert this_host_counterpart(str(old), live_service=True) == str(expected)
    # Unmanaged and own-host paths are unchanged either way.
    plain = tmp_path / "flowcept" / "settings.yaml"
    assert this_host_counterpart(str(plain), live_service=True) == str(plain)
    own = _settings(tmp_path, "gpub099")
    assert this_host_counterpart(str(own), live_service=True) == str(own)


@pytest.mark.usefixtures("on_gpub099")
def test_flowcept_config_without_this_hosts_install_reads_as_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.gact.provenance.flowcept import FlowceptProvenanceProvider

    old = _settings(tmp_path, "gpub081")
    monkeypatch.setenv("FLOWCEPT_SETTINGS_PATH", str(old))
    config = factory._flowcept_config()
    assert "gpub099" in config.settings_path
    with pytest.raises(FileNotFoundError, match="install/start Flowcept on this host"):
        FlowceptProvenanceProvider(config)


def test_flowcept_refuses_settings_other_than_the_ones_it_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flowcept reads settings once at import; a new file needs a restart, said so."""
    import sys
    import types

    from clio_agent.gact.provenance.flowcept import FlowceptProvenanceProvider

    first = _settings(tmp_path, "gpub081")
    second = _settings(tmp_path, "gpub099")
    loaded = types.ModuleType("flowcept.configs")
    loaded.SETTINGS_PATH = str(first)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flowcept.configs", loaded)
    with pytest.raises(RuntimeError, match="restart CLIO"):
        FlowceptProvenanceProvider(FlowceptProviderConfig(settings_path=str(second)))
