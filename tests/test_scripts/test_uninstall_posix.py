"""The POSIX uninstaller removes everything the installer and Desktop leave behind.

It used to keep ``$PREFIX/libexec/clio`` (so the prefix never went away), had no
way to drop the multi-gigabyte cache root, and had no Desktop path on macOS.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

UNINSTALL = Path(__file__).resolve().parents[2] / "install" / "uninstall.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX uninstaller")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cache_root(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "clio-agent"
    return home / ".cache" / "clio-agent"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CLIO_", "XDG_"))}
    env.update(
        HOME=str(tmp_path / "home"),
        CLIO_PREFIX=str(tmp_path / "prefix"),
        CLIO_BIN_DIR=str(tmp_path / "bin"),
        CLIO_PORT=str(_free_port()),
        **extra,
    )
    return env


def _installed(tmp_path: Path) -> Path:
    prefix = tmp_path / "prefix"
    (prefix / "libexec").mkdir(parents=True)
    (prefix / "libexec" / "clio").write_text("#!/bin/sh\n")
    (prefix / "uninstall.sh").write_text("#!/bin/sh\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "clio").write_text("#!/bin/sh\n")
    cache = _cache_root(tmp_path / "home")
    (cache / "document-office").mkdir(parents=True)
    (cache / "document-office" / "soffice").write_text("x")
    return prefix


def _uninstall(env: dict[str, str], *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(UNINSTALL), "--yes", *flags],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def test_uninstall_removes_the_release_launcher_and_the_prefix(tmp_path: Path) -> None:
    prefix = _installed(tmp_path)

    result = _uninstall(_env(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (prefix / "libexec" / "clio").exists()
    assert not prefix.exists()
    assert not (tmp_path / "bin" / "clio").exists()
    assert _cache_root(tmp_path / "home").is_dir(), "caches are kept without --purge-caches"
    assert "pass --purge-caches to remove" in result.stdout


def test_purge_caches_removes_the_agent_cache_root(tmp_path: Path) -> None:
    _installed(tmp_path)

    result = _uninstall(_env(tmp_path), "--purge-caches")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _cache_root(tmp_path / "home").exists()


def test_unknown_flag_names_the_supported_ones(tmp_path: Path) -> None:
    result = _uninstall(_env(tmp_path), "--everything")

    assert result.returncode == 2
    assert "--purge-caches" in result.stderr and "--desktop" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="CLIO Desktop removal is macOS-only")
@pytest.mark.parametrize("purge", [False, True])
def test_desktop_removes_the_app_and_keeps_support_without_purge(
    tmp_path: Path, purge: bool
) -> None:
    _installed(tmp_path)
    home = tmp_path / "home"
    app = tmp_path / "Applications" / "CLIO Desktop.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    library = home / "Library"
    support = library / "Application Support" / "ai.iowarp.clio.desktop"
    logs = library / "Logs" / "ai.iowarp.clio.desktop"
    for folder in (support, logs):
        folder.mkdir(parents=True)

    flags = ["--desktop", *(["--purge"] if purge else [])]
    result = _uninstall(_env(tmp_path, CLIO_DESKTOP_APP=str(app)), *flags)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not app.exists()
    assert not logs.exists()
    assert support.exists() is not purge


@pytest.mark.skipif(sys.platform == "darwin", reason="Linux refuses the macOS-only flag")
def test_desktop_flag_is_refused_off_macos(tmp_path: Path) -> None:
    result = _uninstall(_env(tmp_path), "--desktop")

    assert result.returncode == 2
    assert "macOS" in result.stderr


def _agent_data(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "clio-agent" / "data"
    return home / ".local" / "share" / "clio-agent"


def _perf_name(path: Path) -> str:
    return str(path).replace("/", "_").replace(" ", "_") + "_storage.bin_node0.perf"


def _core_home(tmp_path: Path, seed: str) -> Path:
    prefix = _installed(tmp_path)
    data = prefix / "clio-agent" / ".venv" / "lib" / "python3.14" / "site-packages"
    data = data / "iowarp_core" / "data"
    data.mkdir(parents=True)
    (data / "clio_default.yaml").write_text("# IOWarp Default Configuration\n")
    core = tmp_path / "home" / ".clio"
    (core / "bdev_perf").mkdir(parents=True)
    (core / "clio.yaml").write_text(seed)
    agent_data = _agent_data(tmp_path / "home")
    (core / "bdev_perf" / _perf_name(agent_data / "cte" / "hosts" / "h")).write_text("x")
    return core


def test_purge_removes_clio_cores_unedited_seed_and_clio_perf_stats(tmp_path: Path) -> None:
    core = _core_home(tmp_path, "# IOWarp Default Configuration\n")

    result = _uninstall(_env(tmp_path), "--purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not core.exists(), sorted(p.name for p in core.rglob("*"))


def test_purge_keeps_an_edited_clio_core_config_and_foreign_perf_stats(tmp_path: Path) -> None:
    core = _core_home(tmp_path, "# someone's own clio-core config\n")
    (core / "bdev_perf" / "_srv_scratch_storage.bin_node0.perf").write_text("x")

    result = _uninstall(_env(tmp_path), "--purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (core / "clio.yaml").is_file()
    assert [p.name for p in (core / "bdev_perf").iterdir()] == [
        "_srv_scratch_storage.bin_node0.perf"
    ]


def test_without_purge_clio_core_files_are_kept(tmp_path: Path) -> None:
    core = _core_home(tmp_path, "# IOWarp Default Configuration\n")

    result = _uninstall(_env(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (core / "clio.yaml").is_file()


def _host_state(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "clio-agent" / "state"
    return home / ".local" / "state" / "clio-agent"


def _config_root(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "clio-agent" / "config"
    return home / ".config" / "clio-agent"


def test_purge_finds_the_native_config_root_without_an_installed_agent(tmp_path: Path) -> None:
    # A Desktop-only machine has no Agent venv to ask; the fallback must still be the
    # platform's real config root (it used to be ~/.config/clio-agent on macOS too).
    _installed(tmp_path)
    config = _config_root(tmp_path / "home")
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("gact: {}\n")

    result = _uninstall(_env(tmp_path), "--purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not config.exists()


def test_purge_removes_the_host_wide_core_records(tmp_path: Path) -> None:
    _installed(tmp_path)
    core = _host_state(tmp_path / "home") / "core-hosts" / "host"
    (core / "clio-runtime.clients").mkdir(parents=True)
    (core / "clio-runtime.lock").write_text("")

    kept = _uninstall(_env(tmp_path))
    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert core.is_dir(), "kept without --purge"

    result = _uninstall(_env(tmp_path), "--purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _host_state(tmp_path / "home").exists(), result.stdout + result.stderr
    if sys.platform == "darwin":
        assert not (tmp_path / "home" / "Library" / "Application Support" / "clio-agent").exists()


def test_purge_keeps_the_core_records_of_a_running_daemon(tmp_path: Path) -> None:
    _installed(tmp_path)
    core = _host_state(tmp_path / "home") / "core-hosts" / "host"
    core.mkdir(parents=True)
    (core / "clio-runtime.pid").write_text(str(os.getpid()))

    result = _uninstall(_env(tmp_path), "--purge")

    assert result.returncode == 0, result.stdout + result.stderr
    assert core.is_dir()
    assert "is running; kept" in result.stderr
