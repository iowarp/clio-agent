"""The managed CLI uses fresh CLIO auth and cannot escape source permissions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.storage import github_tool
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.service import StorageService
from clio_agent.runtime import github_cli


@pytest.mark.parametrize(
    "arguments",
    [
        ["auth", "token"],
        ["auth", "status", "--show-token"],
        ["extension", "exec", "unsafe"],
        ["api", "repos/other/repo/contents"],
        ["api", "https://api.github.com/repos/iowarp/clio-agent"],
        ["api", "repos/iowarp/clio-agent/contents/../secrets"],
        ["api", "repos/iowarp/clio-agent/contents/%252e%252e/secrets"],
        ["api", "repos/iowarp/clio-agent/contents", "--input", "private.json"],
        ["api", "repos/iowarp/clio-agent/actions/secrets"],
        ["api", "repos/other/repo/releases/latest"],
        ["api", "repos/iowarp/clio-agent/releases", "--method", "POST"],
        ["api", "repos/iowarp/clio-agent/releases?per_page=100"],
        ["api", "repos/iowarp/clio-agent/releases/tags/../secrets"],
        ["api", "repos/iowarp/clio-agent/releases/assets/123"],
    ],
)
def test_unsafe_cli_arguments_are_rejected(tmp_path: Path, arguments: list[str]) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    record = service.create(
        "w",
        CreateSource(provider="github", root="https://github.com/iowarp/clio-agent", label="Repo"),
    )
    with pytest.raises((ValueError, PermissionError)):
        github_tool.source_arguments(record, arguments)


def test_folder_and_revision_limits_apply_to_the_cli(tmp_path: Path) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    record = service.create(
        "w",
        CreateSource(
            provider="github",
            root="https://github.com/iowarp/clio-agent/tree/develop/docs",
            label="Docs",
        ),
    )
    with pytest.raises(PermissionError):
        github_tool.source_arguments(record, ["api", "repos/iowarp/clio-agent/contents/src"])
    with pytest.raises(ValueError):
        github_tool.source_arguments(record, ["repo", "view"])
    for endpoint in ("releases", "releases/latest", "releases/tags/v1.0.0", "releases/123"):
        with pytest.raises(PermissionError, match="source read scope"):
            github_tool.source_arguments(record, ["api", f"repos/iowarp/clio-agent/{endpoint}"])
    result = github_tool.source_arguments(record, ["api", "repos/iowarp/clio-agent/contents/docs"])
    assert result[-2:] == ["--method", "GET"]
    assert result[1].endswith("?ref=develop")


def test_release_reads_are_scoped_gets_with_publication_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository release metadata uses the selected CLIO grant and retains its facts."""
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    record = service.create(
        "w",
        CreateSource(
            provider="github",
            root="https://github.com/iowarp/clio-agent/tree/develop",
            label="Repo",
        ),
    )
    monkeypatch.setattr(service.auth, "connected", lambda row: True)
    monkeypatch.setattr(service.auth, "token", lambda row: "private-grant")
    release = {
        "tag_name": "v1.0.0-beta.1",
        "draft": False,
        "prerelease": True,
        "published_at": "2026-10-01T12:00:00Z",
        "body": "Published notes",
        "html_url": "https://github.com/iowarp/clio-agent/releases/tag/v1.0.0-beta.1",
    }
    stable = {
        **release,
        "tag_name": "v1.0.0",
        "prerelease": False,
        "html_url": "https://github.com/iowarp/clio-agent/releases/tag/v1.0.0",
    }
    draft = {
        **release,
        "tag_name": "v1.0.0-beta.2",
        "draft": True,
        "published_at": None,
        "html_url": "https://github.com/iowarp/clio-agent/releases/tag/v1.0.0-beta.2",
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        if arguments[1].endswith("/releases"):
            body: Any = [draft, release, stable]
        else:
            body = stable if arguments[1].endswith("/latest") else release
        return subprocess.CompletedProcess(arguments, 0, json.dumps(body), "")

    monkeypatch.setattr(github_tool, "run_github_cli", run)
    for endpoint in (
        "releases",
        "releases/latest",
        "releases/tags/v1.0.0-beta.1",
        "releases/123",
    ):
        path = f"repos/iowarp/clio-agent/{endpoint}"
        result = github_tool.source_cli(service, record, ["api", path])
        assert calls[-1][0] == ["api", path, "--method", "GET"]
        assert calls[-1][1]["token"] == "private-grant"
        assert result["source_id"] == record.source.id
        if endpoint == "releases":
            assert result["result"] == [draft, release, stable]
        else:
            assert result["result"] == (stable if endpoint == "releases/latest" else release)
        assert "private-grant" not in str(result)


def test_sign_out_stops_later_cli_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    record = service.create(
        "w",
        CreateSource(provider="github", root="https://github.com/iowarp/clio-agent", label="Repo"),
    )
    connected = True
    monkeypatch.setattr(service.auth, "connected", lambda row: connected)
    monkeypatch.setattr(service.auth, "token", lambda row: "private-grant")
    calls: list[dict[str, Any]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(arguments, 0, '{"name":"clio-agent"}', "")

    monkeypatch.setattr(github_tool, "run_github_cli", run)
    result = github_tool.source_cli(service, record, ["repo", "view"])
    assert calls[0]["token"] == "private-grant" and "private-grant" not in str(result)
    connected = False
    with pytest.raises(PermissionError, match="Sign in"):
        github_tool.source_cli(service, record, ["repo", "view"])
    with pytest.raises(PermissionError, match="Sign in"):
        github_tool.source_cli(service, record, ["api", "repos/iowarp/clio-agent/releases/latest"])
    assert len(calls) == 1


def test_process_receives_only_private_clio_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "another-host-token")
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setattr(github_cli, "ensure_github_cli", lambda: tmp_path / "verified-gh")
    captured: dict[str, Any] = {}

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, "private-grant", "private-grant")

    monkeypatch.setattr(github_cli.subprocess, "run", run)
    result = github_cli.run_github_cli(
        ["api", "user"], token="private-grant", config_root=tmp_path / "isolated"
    )
    assert captured["env"]["GH_TOKEN"] == "private-grant"
    assert "GITHUB_TOKEN" not in captured["env"] and "GH_DEBUG" not in captured["env"]
    assert captured["env"]["GH_CONFIG_DIR"] == str(tmp_path / "isolated")
    assert "private-grant" not in result.stdout + result.stderr
