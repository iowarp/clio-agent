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
            if "gpub099" not in config.settings_path:
                raise ConnectionError("Connection refused (previous node's Redis)")

        def close(self) -> None:
            return None

    monkeypatch.setattr(flowcept_module, "FlowceptProvenanceProvider", _Recording)
    monkeypatch.setenv("FLOWCEPT_SETTINGS_PATH", str(old))
    provider: Any = factory._deferred_flowcept(factory._flowcept_config())
    assert not provider.attached
    assert seen == [str(old)]

    new = _settings(tmp_path, "gpub099")
    provider._next_attempt = 0.0
    assert provider.recheck()
    assert seen[-1] == str(new)
