"""Pinned downloads, bounded extraction and cache repair for CLIO-managed gh."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from clio_agent.runtime import github_cli


@pytest.fixture
def download(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("gh_test/bin/gh.exe", b"verified-test-executable")
        package.writestr("../../untrusted", b"never extract")
    payload = archive.getvalue()
    monkeypatch.setattr(github_cli.sys, "platform", "win32")
    monkeypatch.setattr(github_cli.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        github_cli,
        "ASSETS",
        {("win32", "amd64"): ("windows_amd64.zip", hashlib.sha256(payload).hexdigest())},
    )
    requests: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=payload)

    client_type = httpx.Client
    monkeypatch.setattr(
        github_cli.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    return requests


def test_only_verified_executable_is_extracted_and_cache_is_repaired(
    tmp_path: Path, download: list[str]
) -> None:
    executable = github_cli.ensure_github_cli(cache_root=tmp_path / "cache")
    assert executable.read_bytes() == b"verified-test-executable"
    assert not (tmp_path / "untrusted").exists()
    assert github_cli.ensure_github_cli(cache_root=tmp_path / "cache") == executable
    assert len(download) == 1
    executable.write_bytes(b"tampered")
    assert (
        github_cli.ensure_github_cli(cache_root=tmp_path / "cache").read_bytes()
        == b"verified-test-executable"
    )
    assert len(download) == 2


def test_a_download_with_the_wrong_digest_never_installs(
    tmp_path: Path, download: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_cli, "ASSETS", {("win32", "amd64"): ("windows_amd64.zip", "0" * 64)})
    with pytest.raises(ValueError, match="pinned SHA-256"):
        github_cli.ensure_github_cli(cache_root=tmp_path)
    assert not list(tmp_path.rglob("gh.exe"))


def test_shell_lookup_never_downloads_and_rejects_changed_cached_bytes(
    tmp_path: Path, download: list[str]
) -> None:
    cache = tmp_path / "cache"
    assert github_cli.installed_github_cli(cache_root=cache) is None
    assert download == []
    executable = github_cli.ensure_github_cli(cache_root=cache)
    assert github_cli.installed_github_cli(cache_root=cache) == executable
    executable.write_bytes(b"tampered")
    assert github_cli.installed_github_cli(cache_root=cache) is None
    assert len(download) == 1
    assert executable.read_bytes() == b"tampered"


def test_shell_lookup_on_an_unsupported_platform_is_unavailable(
    tmp_path: Path, download: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_cli.platform, "machine", lambda: "unsupported")
    assert github_cli.installed_github_cli(cache_root=tmp_path) is None
    assert download == []
