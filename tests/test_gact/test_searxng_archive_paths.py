"""Extract pinned source archives on Windows extended-length paths safely."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import searxng_hook


def archive(path: Path, *, escape: bool = False) -> Path:
    """Write directory entries exactly as the upstream POSIX source tar uses them."""
    result = path / "source.tar.gz"
    with tarfile.open(result, "w:gz") as writer:
        folder = tarfile.TarInfo("searxng-commit/utils/templates/etc/uwsgi/apps-available")
        folder.type = tarfile.DIRTYPE
        writer.addfile(folder)
        member = tarfile.TarInfo(
            "searxng-commit/../outside.txt" if escape else folder.name + "/searxng.ini"
        )
        content = b"[uwsgi]\n"
        member.size = len(content)
        writer.addfile(member, io.BytesIO(content))
    return result


def test_directory_metadata_works_with_windows_extended_paths(tmp_path: Path) -> None:
    """Python's directory-metadata pass must not receive mixed long-path separators."""
    source = archive(tmp_path)
    destination = tmp_path / "service with spaces" / "source"
    destination.mkdir(parents=True)
    if os.name == "nt":
        destination = Path("\\\\?\\" + str(destination.resolve()))
    with tarfile.open(source, "r:gz") as reader:
        reader.extractall(destination, members=searxng_hook._members(reader), filter="data")
    assert (
        destination / "utils/templates/etc/uwsgi/apps-available/searxng.ini"
    ).read_bytes() == b"[uwsgi]\n"


def test_native_member_paths_keep_tar_data_filter_escape_protection(tmp_path: Path) -> None:
    source = archive(tmp_path, escape=True)
    destination = tmp_path / "source"
    with tarfile.open(source, "r:gz") as reader:
        with pytest.raises(tarfile.OutsideDestinationError):
            reader.extractall(destination, members=searxng_hook._members(reader), filter="data")
    assert not (tmp_path / "outside.txt").exists()
