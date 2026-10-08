"""Stable and beta installs must use actual release refs at every GitHub boundary."""

from __future__ import annotations

import pytest

from clio_agent.gact.infrastructure.clio_agent_deploy import install_command
from clio_agent.gact.infrastructure.release_tag import release_tag


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.9.4.24", "v0.9.4.24"),
        ("0.9.5", "v0.9.5"),
        ("0.9.5b1", "v0.9.5-beta.1"),
        ("0.9.5b12", "v0.9.5-beta.12"),
        ("0.9.5-beta.1", "v0.9.5-beta.1"),
        ("0.9.5b5.post1", "v0.9.5-beta.5.1"),
        ("0.9.5-beta.5.1", "v0.9.5-beta.5.1"),
    ],
)
def test_release_refs_keep_registry_and_github_versions_separate(version: str, tag: str) -> None:
    """The bootstrap URL and installer environment use the same GitHub release."""

    assert release_tag(version) == tag
    command = install_command("/tmp/clio", version)
    assert command.args[-4:] == [
        version,
        f"https://pypi.org/pypi/clio-agent/{version}/json",
        f"https://raw.githubusercontent.com/iowarp/clio-agent/{tag}/install/install.sh",
        tag,
    ]


@pytest.mark.parametrize("version", ["0.9.5.dev1", "0.9.5+local", "main", "../main"])
def test_unreleased_versions_are_actionable_errors(version: str) -> None:
    """Do not invent URLs for unrepresentable development versions."""

    with pytest.raises(ValueError, match="deploy from a released CLIO"):
        install_command("/tmp/clio", version)
