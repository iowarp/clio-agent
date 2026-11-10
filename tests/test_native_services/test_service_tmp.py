"""A service's TMPDIR stays short enough for the Unix sockets its workers bind."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import native_service_platform
from clio_agent.gact.infrastructure.native_service_platform import service_tmp, worker_environment


def test_a_shallow_service_root_keeps_its_own_tmp(tmp_path: Path) -> None:
    root = Path("/srv/clio-vllm")
    assert service_tmp(root) == root / "tmp"


def test_a_deep_service_root_gets_a_short_private_tmp(tmp_path: Path) -> None:
    """On POSIX a deep root's TMPDIR is a short private directory for the workers' sockets.

    Windows has neither the Unix-socket limit nor a POSIX owner id, so there the same deep
    root keeps its own ``root/tmp``; either way the worker environment points at it."""
    root = tmp_path / ("deep-" * 20) / "clio-vllm-spotter"
    short = service_tmp(root)
    if os.name == "posix":
        assert len(str(short)) < 64
        assert short.is_dir() and short.stat().st_mode & 0o077 == 0
    else:
        assert short == root / "tmp"
    assert service_tmp(root) == short  # stable for the same root
    env = worker_environment(root, {}, {"PATH": "/usr/bin"})
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(short)


def test_a_deep_root_keeps_its_own_tmp_where_there_are_no_unix_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has neither the socket path limit nor a POSIX owner id to check."""
    monkeypatch.setattr(native_service_platform, "_POSIX", False)
    root = tmp_path / ("deep-" * 20) / "clio-vllm-spotter"
    assert service_tmp(root) == root / "tmp"
