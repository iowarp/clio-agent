"""Unit tests for the git clone-source normalizer (iowarp/clio-agent#903)."""

from __future__ import annotations

import pytest

from clio_agent.gact.git_source import normalize_git_clone_source


def test_windows_drive_file_uri_becomes_local_path() -> None:
    """``file:///C:/...`` drops the URI authority slash to a drive path."""
    assert (
        normalize_git_clone_source("file:///C:/Users/alice/marketplace")
        == "C:/Users/alice/marketplace"
    )


def test_windows_drive_root_file_uri() -> None:
    """A drive URI with no trailing path still normalizes to ``C:``."""
    assert normalize_git_clone_source("file:///C:") == "C:"


def test_posix_file_uri_becomes_absolute_path() -> None:
    """``file:///home/x`` becomes the plain POSIX absolute path."""
    assert normalize_git_clone_source("file:///home/user/repo") == "/home/user/repo"


def test_localhost_authority_is_treated_as_local() -> None:
    """A ``localhost`` authority is not a remote host; the path is local."""
    assert normalize_git_clone_source("file://localhost/home/user/repo") == "/home/user/repo"


def test_percent_encoded_path_is_decoded() -> None:
    """URI percent-encoding (e.g. a space) is decoded to a real path."""
    assert normalize_git_clone_source("file:///tmp/my%20repo") == "/tmp/my repo"


def test_unc_file_uri_becomes_double_slash_path() -> None:
    """``file://server/share`` maps to a git-acceptable ``//server/share`` UNC."""
    assert (
        normalize_git_clone_source("file://server/share/repo") == "//server/share/repo"
    )


@pytest.mark.parametrize(
    "source",
    [
        "https://github.com/iowarp/clio-agent-marketplace.git",
        "ssh://git@github.com/iowarp/repo.git",
        "git@github.com:iowarp/repo.git",
        "/home/user/local/checkout",
        "C:/Users/alice/checkout",
    ],
)
def test_non_file_sources_pass_through_unchanged(source: str) -> None:
    """Non-``file`` schemes and plain local paths are returned verbatim."""
    assert normalize_git_clone_source(source) == source


@pytest.mark.parametrize("source", ["file://", "file://server"])
def test_malformed_file_uri_raises(source: str) -> None:
    """A ``file://`` URI with no path is a structured error, never silent."""
    with pytest.raises(ValueError, match="malformed file:// URI"):
        normalize_git_clone_source(source)
