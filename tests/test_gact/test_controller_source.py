"""A remote CLIO runs the code its controller runs: a release, or the same commit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import controller_source
from clio_agent.gact.infrastructure.drivers import _clio_agent_plan
from clio_agent.gact.infrastructure.models import InfrastructureTarget

# The real lookup: the suite's autouse fixture stands a released controller in for it.
controller_commit = controller_source.controller_commit


def _manifest(tmp_path: Path, source: dict | None) -> Path:
    data = {"schema": 1, "exec": ["python"]}
    if source is not None:
        data["source"] = source
    (tmp_path / "runtime.json").write_text(json.dumps(data))
    return tmp_path


@pytest.mark.parametrize(
    ("source", "commit"),
    [
        ({"commit": "abc123", "release": False}, "abc123"),
        ({"commit": "abc123", "release": True}, None),
        (None, None),  # a runtime built before it recorded its source
    ],
)
def test_a_bundled_runtime_names_its_commit_unless_it_is_a_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: dict | None, commit: str | None
) -> None:
    monkeypatch.setenv("GACT_BUNDLED_RUNTIME_DIR", str(_manifest(tmp_path, source)))
    assert controller_commit() == commit


@pytest.mark.parametrize(("tag", "commit"), [("", "f00d"), ("v0.9.5-beta.8", None)])
def test_a_checkout_names_its_commit_unless_it_sits_on_a_release_tag(
    monkeypatch: pytest.MonkeyPatch, tag: str, commit: str | None
) -> None:
    monkeypatch.delenv("GACT_BUNDLED_RUNTIME_DIR", raising=False)
    answers = {"rev-parse": "f00d", "describe": tag}
    monkeypatch.setattr(controller_source, "_git", lambda _root, *args: answers[args[0]])
    assert controller_commit() == commit


def _target() -> InfrastructureTarget:
    return InfrastructureTarget.model_validate(
        {
            "id": "t",
            "label": "ares",
            "kind": "ssh",
            "install_root": "/home/me/clio",
            "created_at": "2026-10-27T00:00:00Z",
            "updated_at": "2026-10-27T00:00:00Z",
        }
    )


def test_a_source_controller_deploys_its_own_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.infrastructure.drivers.controller_commit", lambda: "c0ffee"
    )
    install = next(
        spec
        for spec in _clio_agent_plan("install", _target()).commands
        if spec.args[1].startswith("# clio-deploy:install")
    )
    assert install.args[-1] == "c0ffee"
    assert (
        "https://raw.githubusercontent.com/iowarp/clio-agent/c0ffee/install/install.sh"
        in install.args
    )


def test_a_release_controller_deploys_its_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("clio_agent.gact.infrastructure.drivers.controller_commit", lambda: None)
    install = next(
        spec
        for spec in _clio_agent_plan("install", _target()).commands
        if spec.args[1].startswith("# clio-deploy:install")
    )
    assert install.args[-1] == ""


def test_a_source_controller_compares_remote_agents_by_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim reports a source install as ``<version>+g<commit>``; the controller matches."""
    monkeypatch.setattr(controller_source, "controller_commit", lambda: "0123456789abcdef")
    assert controller_source.deployment_version("0.9.5b8") == "0.9.5b8+g0123456789ab"
    monkeypatch.setattr(controller_source, "controller_commit", lambda: None)
    assert controller_source.deployment_version("0.9.5b8") == "0.9.5b8"
