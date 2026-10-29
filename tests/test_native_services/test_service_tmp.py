"""A service's TMPDIR stays short enough for the Unix sockets its workers bind."""

from __future__ import annotations

from pathlib import Path

from clio_agent.gact.infrastructure.native_service_platform import service_tmp, worker_environment


def test_a_shallow_service_root_keeps_its_own_tmp(tmp_path: Path) -> None:
    root = Path("/srv/clio-vllm")
    assert service_tmp(root) == root / "tmp"


def test_a_deep_service_root_gets_a_short_private_tmp(tmp_path: Path) -> None:
    root = tmp_path / ("deep-" * 20) / "clio-vllm-spotter"
    short = service_tmp(root)
    assert len(str(short)) < 64
    assert short.is_dir() and short.stat().st_mode & 0o077 == 0
    assert service_tmp(root) == short  # stable for the same root
    env = worker_environment(root, {}, {"PATH": "/usr/bin"})
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(short)
