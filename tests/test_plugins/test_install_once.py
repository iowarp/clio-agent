"""An install reads each plugin once, downloads a pinned GitHub commit and copies in parallel.

The first send on a clean home waits for the default marketplace's install, so its work is
done once: the snapshot's packages load their files once for the untrusted and the trusted
read, a pinned GitHub commit is one archive download instead of a clone, and the snapshot
copy overlaps its files.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.plugins import loader, sources
from clio_agent.plugins.loader import load_plugin, loading_once
from clio_agent.plugins.marketplace import probe_marketplace, read_marketplace
from clio_agent.plugins.package_files import ignored_package_names
from clio_agent.plugins.sources import cache_key, fetch, remove_cached
from clio_agent.plugins.store import _copy_files

from .test_sources_without_git import COMMIT, MARKET, _archive

URL = "https://github.com/o/r"
PLUGIN = '{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"p","version":"1.0.0"}'


def _plugin(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(PLUGIN, encoding="utf-8")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": {"command": "run-s", "args": ["--x"]}}}), encoding="utf-8"
    )
    return root


def _verb(args: list[str]) -> str:
    return next(arg for arg in args if not arg.startswith(("-", "protocol.")))


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Git is installed; each call is recorded and makes a checkout of ``COMMIT``."""

    calls: list[list[str]] = []

    def git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
        if cwd is None:
            return ""
        calls.append(args)
        if _verb(args) == "init":
            (cwd / ".git").mkdir()
        if _verb(args) == "checkout":
            for relative, text in MARKET.items():
                (cwd / relative).parent.mkdir(parents=True, exist_ok=True)
                (cwd / relative).write_text(text, encoding="utf-8")
        return COMMIT if _verb(args) == "rev-parse" else ""

    monkeypatch.setattr(sources, "_git", git)
    return calls


def _serve(monkeypatch: pytest.MonkeyPatch, data: bytes | None) -> list[str]:
    requested: list[str] = []

    def opened(url: str) -> io.BytesIO:
        requested.append(url)
        if data is None:
            raise OSError("codeload is unreachable")
        return io.BytesIO(data)

    monkeypatch.setattr(sources, "_open_archive", opened)
    return requested


def test_a_pinned_github_commit_is_one_archive_download_and_then_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: list[list[str]]
) -> None:
    requested = _serve(monkeypatch, _archive(MARKET))
    tmp_path = tmp_path / "cache"
    first = fetch(URL, cache=tmp_path, commit=COMMIT)
    again = fetch(URL, cache=tmp_path, commit=COMMIT)
    assert git_calls == []
    assert requested == [f"https://codeload.github.com/o/r/tar.gz/{COMMIT}"]
    assert first.path == again.path and again.commit == COMMIT
    assert (first.path / ".claude-plugin/marketplace.json").is_file()
    key = cache_key(URL, COMMIT)
    assert (tmp_path / f".{key}.archive").read_text(encoding="utf-8") == COMMIT
    remove_cached(tmp_path, key)
    assert list(tmp_path.iterdir()) == []


def test_a_pinned_commit_is_cloned_when_its_archive_cannot_be_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: list[list[str]]
) -> None:
    requested = _serve(monkeypatch, None)
    tmp_path = tmp_path / "cache"
    fetched = fetch(URL, cache=tmp_path, commit=COMMIT)
    assert len(requested) == 1
    assert [_verb(call) for call in git_calls] == [
        "init",
        "remote",
        "fetch",
        "checkout",
        "rev-parse",
    ]
    assert (fetched.path / ".git").is_dir() and fetched.commit == COMMIT
    assert [path.name for path in tmp_path.iterdir()] == [fetched.path.name]


def test_a_ref_or_another_host_is_still_cloned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_calls: list[list[str]]
) -> None:
    requested = _serve(monkeypatch, _archive(MARKET))
    fetch(URL, cache=tmp_path / "ref", ref="main")
    fetch("https://gitlab.example.invalid/o/r", cache=tmp_path / "other", commit=COMMIT)
    assert requested == []
    assert [_verb(call) for call in git_calls].count("init") == 2


def test_a_snapshot_package_reads_its_files_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[Path] = []
    read_manifest = loader.read_manifest

    def counted(root: Path):  # noqa: ANN202 - the wrapped function's own return
        reads.append(root)
        return read_manifest(root)

    monkeypatch.setattr(loader, "read_manifest", counted)
    inside = _plugin(tmp_path / "store" / "snapshots" / "digest" / "p")
    outside = _plugin(tmp_path / "local" / "p")
    with loading_once(tmp_path / "store" / "snapshots"):
        untrusted = load_plugin(inside, trusted=False)
        trusted = load_plugin(inside, trusted=True)
        load_plugin(outside, trusted=False)
        load_plugin(outside, trusted=False)
    assert reads.count(inside.resolve()) == 1
    assert reads.count(outside.resolve()) == 2
    assert untrusted.mcp_servers == trusted.mcp_servers
    # Each record owns its tables.
    assert untrusted.mcp_servers is not trusted.mcp_servers
    assert untrusted.host is not trusted.host
    load_plugin(inside, trusted=False)
    assert reads.count(inside.resolve()) == 2


def test_a_plugin_without_flows_does_not_load_the_flow_engine(tmp_path: Path) -> None:
    root = _plugin(tmp_path / "p")
    tools = root / "ai.iowarp.clio" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from clio_agent.plugins.loader import load_plugin\n"
        f"load_plugin(Path({str(root)!r}), trusted=True)\n"
        "print('clio_agent.sdk.flow' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-B", "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


def test_the_native_probe_names_the_marketplace_as_the_full_read_does(tmp_path: Path) -> None:
    index = tmp_path / "ai.iowarp.clio" / "marketplace.json"
    index.parent.mkdir(parents=True)
    pin = {
        "name": "wtfp",
        "source": {
            "source": "git-subdir",
            "url": "https://github.com/JaimeCernuda/wtf-p",
            "path": "clio-plugin",
            "sha": "0123456789abcdef0123456789abcdef01234567",
        },
    }
    index.write_text(
        json.dumps({"name": "clio", "description": "d", "plugins": [pin]}), encoding="utf-8"
    )
    _plugin(tmp_path / "plugins" / "p")
    probe = probe_marketplace(tmp_path)
    full = read_marketplace(tmp_path, trusted=False)
    assert (probe.name, probe.format, probe.description) == (
        full.name,
        full.format,
        full.description,
    )
    assert probe.git_plugins == full.git_plugins
    assert probe.plugins == []


def test_the_parallel_snapshot_copy_matches_copytree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for relative in ["a.md", "skills/s/SKILL.md", "tools/run.sh", "deep/x/y/z.txt"]:
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(relative, encoding="utf-8")
    for ignored in [".git/HEAD", "node_modules/m.js", "tools/__pycache__/t.pyc", "skills/x.pyc"]:
        (source / ignored).parent.mkdir(parents=True, exist_ok=True)
        (source / ignored).write_text("generated", encoding="utf-8")
    (source / "empty").mkdir()
    os.utime(source / "a.md", (1_000_000, 1_000_000))
    expected = tmp_path / "expected"
    shutil.copytree(source, expected, ignore=ignored_package_names)
    actual = tmp_path / "actual"
    _copy_files(str(source), str(actual))

    def tree(root: Path) -> dict[str, tuple[bool, str, int]]:
        return {
            path.relative_to(root).as_posix(): (
                path.is_dir(),
                "" if path.is_dir() else path.read_text(encoding="utf-8"),
                0 if path.is_dir() else path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
        }

    assert tree(actual) == tree(expected)
    assert (actual / "a.md").stat().st_mtime == 1_000_000
