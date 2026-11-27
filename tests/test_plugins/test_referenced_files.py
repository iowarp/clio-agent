"""``referenced_files`` reads URL args as URLs and never asks another network share.

Splitting ``git+https://host/…`` on ``:`` once made ``//host/…``, which Windows reads
as the share ``\\\\host\\…``: resolving it waited on the network for many seconds.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from clio_agent.plugins import loader
from clio_agent.plugins.manifest import within

RELAY_LIKE = {
    "relay": {
        "command": "uv",
        "args": [
            "run",
            "--project",
            "${PLUGIN_ROOT}",
            "--with",
            "clio-relay @ git+https://github.com/iowarp/clio-relay@06a14cd423e0",
            "--with",
            "git+ssh://git@github.com:iowarp/jarvis-cd.git#egg=jarvis&subdirectory=src",
            "python",
            "${PLUGIN_ROOT}/bin/serve.py",
        ],
        "env": {
            "PYTHONPATH": "${PLUGIN_ROOT}/lib:${PLUGIN_ROOT}/vendor",
            "INDEX": "https://pypi.org/simple",
            "DATA": "${PLUGIN_DATA}/state",
        },
    }
}


def _previous(root: Path, servers: dict) -> set[Path]:
    """The split before URLs were recognised: every word split on ``:``."""

    texts: list[str] = []
    for entry in servers.values():
        texts += [str(entry.get("command") or ""), str(entry.get("cwd") or "")]
        texts += [str(arg) for arg in entry.get("args") or ()]
        texts += [str(value) for value in (entry.get("env") or {}).values()]
    found: set[Path] = set()
    for text in texts:
        for token in loader._ROOT_TOKENS:
            text = text.replace(token, str(root))
        for piece in re.split(r"[\s=:,;]+", text):
            if not piece:
                continue
            candidate = Path(piece) if Path(piece).is_absolute() else root / piece
            if "github.com" in piece or "pypi.org" in piece:
                continue  # resolving a //host piece is the very wait being removed
            if not within(root, candidate):
                continue
            if candidate.is_file():
                found.add(candidate.resolve())
            elif candidate.is_dir() and candidate.resolve() != root.resolve():
                found.update(p.resolve() for p in candidate.rglob("*") if p.is_file())
    return found


@pytest.fixture
def root(tmp_path: Path) -> Path:
    plugin = tmp_path / "plugin"
    for name in ("bin/serve.py", "lib/a.py", "vendor/b/c.py"):
        (plugin / name).parent.mkdir(parents=True, exist_ok=True)
        (plugin / name).write_text("x", encoding="utf-8")
    return plugin


def test_url_args_are_never_split_into_host_paths(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def spy(base: Path, candidate: Path) -> bool:
        asked.append(str(candidate))
        return within(base, candidate)

    monkeypatch.setattr(loader, "within", spy)
    found = loader.referenced_files(root, RELAY_LIKE)

    assert not [c for c in asked if "github.com" in c or "pypi.org" in c]
    resolved = root.resolve()
    assert found == {
        resolved / "bin" / "serve.py",
        resolved / "lib" / "a.py",
        resolved / "vendor" / "b" / "c.py",
    }


def test_the_files_found_are_the_previous_split_s(root: Path) -> None:
    # The executable digest is built from these files: a trusted digest stays valid.
    assert loader.referenced_files(root, RELAY_LIKE) == _previous(root, RELAY_LIKE)


def test_a_file_url_names_its_file(root: Path) -> None:
    servers = {"s": {"command": "run", "args": [(root / "bin" / "serve.py").as_uri()]}}
    assert loader.referenced_files(root, servers) == {(root / "bin" / "serve.py").resolve()}


def test_a_remote_file_url_names_no_file(root: Path) -> None:
    servers = {"s": {"command": "run", "args": ["file://example.com/share/bin/serve.py"]}}
    assert loader.referenced_files(root, servers) == set()


@pytest.mark.parametrize(
    ("path", "share"),
    [
        (PureWindowsPath(r"\\github.com\iowarp\clio-relay@06a1"), True),
        (PureWindowsPath("//github.com/iowarp/clio-relay"), True),
        (PureWindowsPath(r"\\?\UNC\host\share\x"), True),
        (PureWindowsPath(r"\\?\D:\x"), False),
        (PureWindowsPath(r"\\.\pipe\x"), False),
        (PureWindowsPath(r"D:\x"), False),
        (PureWindowsPath(r"\x"), False),
        (PurePosixPath("//github.com/iowarp"), False),
        (PurePosixPath("/srv/x"), False),
    ],
)
def test_a_network_share_is_told_apart_lexically(path, share: bool) -> None:
    assert loader._network_share(path) is share


def test_a_network_piece_is_never_resolved(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def spy(base: Path, candidate: Path) -> bool:
        asked.append(str(candidate))
        return within(base, candidate)

    monkeypatch.setattr(loader, "within", spy)
    monkeypatch.setattr(loader, "_network_share", lambda path: "far-host" in str(path))
    monkeypatch.setattr(loader, "_drive", lambda path: "far" if "far-host" in str(path) else "")
    servers = {"s": {"command": "run", "args": ["//far-host/share/x", "bin/serve.py"]}}

    assert loader.referenced_files(root, servers) == {(root / "bin" / "serve.py").resolve()}
    assert not [c for c in asked if "far-host" in c]
