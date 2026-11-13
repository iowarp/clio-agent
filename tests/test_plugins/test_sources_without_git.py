"""A machine with no git still fetches a GitHub source, from GitHub's archive (#739).

A Mac without the Command Line Tools whose install came from PyPI has no git, and the
default marketplace must still install during the install.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from clio_agent.plugins import sources
from clio_agent.plugins.sources import GitMissingError, SourceError, fetch
from clio_agent.plugins.store import PluginStore, StoreError
from clio_agent.runtime.git_executable import GitUnavailableError

COMMIT = "ecf7e955eeb88ed539ea154f042a5eaa04a3946c"
MARKET = {".claude-plugin/marketplace.json": json.dumps({"name": "m", "plugins": []})}


def _archive(files: dict[str, str], commit: str = COMMIT, *, extra: tuple = ()) -> bytes:
    """A tarball laid out as GitHub's: one ``<repo>-<commit>/`` folder, the commit in the
    global header's ``comment``."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
    ) as bundle:
        top = tarfile.TarInfo(f"r-{commit}/")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        bundle.addfile(top)
        for relative, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"r-{commit}/{relative}")
            info.size = len(data)
            info.mode = 0o755 if relative.endswith(".sh") else 0o644
            bundle.addfile(info, io.BytesIO(data))
        for info in extra:
            bundle.addfile(info, io.BytesIO(b"x" * info.size) if info.size else None)
    return buffer.getvalue()


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No git on this machine; returns the archive URLs requested, served by ``serve``."""

    def missing(*_args: object, **_kwargs: object) -> None:
        raise GitUnavailableError("git is not installed (/usr/bin/git is the placeholder)")

    monkeypatch.setattr(sources, "run_git", missing)
    return []


def _serve(monkeypatch: pytest.MonkeyPatch, requested: list[str], data: bytes) -> None:
    def opened(url: str) -> io.BytesIO:
        requested.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(sources, "_open_archive", opened)


def test_the_pinned_marketplace_installs_from_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    files = {**MARKET, "tools/run.sh": "#!/bin/sh\n", "docs/a/b.md": "deep\n"}
    _serve(monkeypatch, no_git, _archive(files))
    store = PluginStore(tmp_path / "store")
    summary = store.install(
        "https://github.com/iowarp/clio-agent-marketplace.git", commit=COMMIT, default=True
    )
    assert summary["marketplace"] == "m"
    assert no_git == [f"https://codeload.github.com/iowarp/clio-agent-marketplace/tar.gz/{COMMIT}"]
    entry = store.marketplaces()["m"]
    assert entry["commit"] == COMMIT and entry["trusted"]
    snapshot = Path(entry["snapshot"])
    assert (snapshot / "docs/a/b.md").read_text() == "deep\n"
    checkout = (
        tmp_path
        / "store"
        / "sources"
        / sources.cache_key("https://github.com/iowarp/clio-agent-marketplace.git", COMMIT)
    )
    assert (checkout / "tools/run.sh").stat().st_mode & 0o100
    assert not list((tmp_path / "store" / "sources").glob(".*partial*"))


def test_an_archive_of_another_commit_than_the_pin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    _serve(monkeypatch, no_git, _archive(MARKET, commit="b" * 40))
    with pytest.raises(SourceError, match="but the pin is"):
        fetch("https://github.com/o/r.git", cache=tmp_path / "cache", commit=COMMIT)
    assert list((tmp_path / "cache").iterdir()) == []


def test_an_archive_member_cannot_land_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    climb = tarfile.TarInfo(f"r-{COMMIT}/../../escaped")
    climb.size = 1
    _serve(monkeypatch, no_git, _archive(MARKET, extra=(climb,)))
    with pytest.raises(SourceError, match="GitHub's archive failed"):
        fetch("https://github.com/o/r", cache=tmp_path / "cache", commit=COMMIT)
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "cache/escaped").exists()
    assert list((tmp_path / "cache").iterdir()) == []


def test_an_archive_past_the_size_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    monkeypatch.setattr(sources, "ARCHIVE_MAX_BYTES", 64)
    _serve(monkeypatch, no_git, _archive({**MARKET, "big": "x" * 100_000}))
    with pytest.raises(SourceError, match="larger than 64 bytes"):
        fetch("https://github.com/o/r", cache=tmp_path, commit=COMMIT)


def test_an_archive_that_names_no_commit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    _serve(monkeypatch, no_git, _archive(MARKET, commit=""))
    with pytest.raises(SourceError, match="names no commit"):
        fetch("https://github.com/o/r", cache=tmp_path, ref="main")


def test_an_unpinned_source_records_the_commit_github_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    _serve(monkeypatch, no_git, _archive(MARKET))
    fetched = fetch("github:o/r", cache=tmp_path, ref="release/1.x")
    assert fetched.commit == COMMIT
    assert no_git == ["https://codeload.github.com/o/r/tar.gz/release/1.x"]


@pytest.mark.parametrize("ref", ["a..b", "bad ref", "x.lock", "/x", "x/", "a@{1}", "a//b"])
def test_a_ref_is_still_checked_without_git(tmp_path: Path, no_git: list[str], ref: str) -> None:
    with pytest.raises(SourceError, match="not a branch or tag name"):
        fetch("https://github.com/o/r.git", cache=tmp_path, ref=ref)


def test_a_source_github_does_not_host_still_needs_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_git: list[str]
) -> None:
    _serve(monkeypatch, no_git, b"")
    with pytest.raises(GitMissingError, match="placeholder"):
        fetch("https://gitlab.example.invalid/o/r.git", cache=tmp_path, commit=COMMIT)
    assert no_git == []
    with pytest.raises(StoreError, match="placeholder"):
        PluginStore(tmp_path / "store").install("https://gitlab.example.invalid/o/r.git")


@pytest.mark.parametrize(
    ("url", "archive"),
    [
        ("https://github.com/o/r.git", "https://codeload.github.com/o/r/tar.gz/HEAD"),
        (
            "https://github.com/JaimeCernuda/wtf-p",
            "https://codeload.github.com/JaimeCernuda/wtf-p/tar.gz/HEAD",
        ),
        ("https://github.com/o/r.js/", "https://codeload.github.com/o/r.js/tar.gz/HEAD"),
        ("https://github.com/o/r/tree/main", None),
        ("git@github.com:o/r.git", None),
        ("https://github.com.evil.example/o/r", None),
    ],
)
def test_only_a_github_https_repository_has_an_archive(url: str, archive: str | None) -> None:
    assert sources.github_archive_url(url, "HEAD") == archive
