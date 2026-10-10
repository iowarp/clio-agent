"""F056: a provenance connection saved on one cluster node follows the deployment to the next.

The stored Flowcept connection row names ``…/services/<host>/flowcept/settings.yaml``
of the node that verified it. On another node of the shared filesystem it must
resolve to this host's copy (28a) or say that Flowcept is not installed here,
never probe the previous node's endpoints with its settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.gact.infrastructure import provenance_connections as connections
from clio_agent.gact.infrastructure import service_paths
from clio_agent.user_config_document import user_config_path, write_document


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("CLIO_USER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service_paths.platform, "node", lambda: "gpub099.delta.example")
    conf.reload()
    write_document(user_config_path(), {"lm": {"model": "retained"}})
    conf.reload()
    return tmp_path


def _deployment(root: Path, host: str) -> tuple[Path, Path]:
    settings = root / "services" / host / "flowcept" / "settings.yaml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(f"host: {host}", encoding="utf-8")
    captures = root / "services" / host / "vllm-attn" / "evidence"
    captures.mkdir(parents=True, exist_ok=True)
    return settings, captures


def _row(settings: Path, captures: Path) -> Any:
    return connections.ProvenanceConnectionInput(
        service_id="flowcept",
        label="flowcept",
        url="http://127.0.0.1:8380",
        settings_path=str(settings),
        attention_files_dir=str(captures),
        capture_attention=True,
    ).record()


def test_row_from_another_node_resolves_to_this_hosts_deployment(isolated: Path) -> None:
    old_settings, old_captures = _deployment(isolated, "gpub081")
    new_settings, new_captures = _deployment(isolated, "gpub099")
    row = _row(old_settings, old_captures)

    here = connections.on_this_host(row)

    assert here.configuration["settings_path"] == str(new_settings)
    assert here.configuration["attention_files_dir"] == str(new_captures)
    verified = here.model_copy(
        update={
            "verification": {
                "revision": connections.connection_revision(here),
                "write_readback": True,
            }
        }
    )
    # Activating the stored (old-node) row with this host's verification works and
    # writes this host's paths.
    connections.activate_connection(
        verified.model_copy(update={"configuration": row.configuration})
    )
    conf.reload()
    assert conf.store().file_value("provenance.agentic.flowcept.settings_path") == str(new_settings)
    assert connections.connection_selected(row)


def test_config_saved_on_another_node_still_reads_as_selected(isolated: Path) -> None:
    old_settings, old_captures = _deployment(isolated, "gpub081")
    _deployment(isolated, "gpub099")
    row = _row(old_settings, old_captures)
    write_document(
        user_config_path(),
        {
            "provenance": {
                "agentic": {
                    "providers": ["jsonl", "flowcept"],
                    "flowcept": {
                        "settings_path": str(old_settings),
                        "persistence_owner": "collector",
                        "privacy": "full",
                    },
                },
                "attention": {"enabled": True, "files_dir": str(old_captures)},
            }
        },
    )
    conf.reload()

    assert connections.connection_selected(row)


def test_flowcept_not_installed_here_is_a_typed_error_not_a_probe(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_settings, old_captures = _deployment(isolated, "gpub081")
    row = _row(old_settings, old_captures)
    monkeypatch.setattr(
        connections.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not probe another node's endpoints"),
    )

    with pytest.raises(ValueError, match="install and start Flowcept on this host"):
        connections.verify_connection(row)
    assert not connections.connection_selected(row)


def test_this_hosts_row_and_cmf_rows_are_unchanged(isolated: Path) -> None:
    settings, captures = _deployment(isolated, "gpub099")
    row = _row(settings, captures)
    assert connections.on_this_host(row) == row
    cmf = connections.ProvenanceConnectionInput(
        service_id="cmf", label="cmf", url="http://127.0.0.1:38380"
    ).record()
    assert connections.on_this_host(cmf) is cmf


def test_attention_folder_of_another_host_is_never_used(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_settings, old_captures = _deployment(isolated, "gpub081")
    new_settings, new_captures = _deployment(isolated, "gpub099")
    new_captures.rmdir()  # Flowcept installed here, the attention vLLM not yet
    row = _row(old_settings, old_captures)
    monkeypatch.setattr(
        connections.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not verify with another node's capture folder"),
    )

    with pytest.raises(ValueError, match="attention folder names another host's deployment"):
        connections.verify_connection(row)
    assert not connections.connection_selected(row)
