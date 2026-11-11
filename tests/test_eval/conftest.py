"""Shared fixtures for the tool-calling evaluation harness tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.eval.tool_calling.fixtures.common import build_history

_SITES = '''"""Module {n}: small helpers with one example of each seeded bug pattern."""


def load(path, retries: int = 3):
    with open(path) as handle:
        return handle.read()


def pick(items, mode="fast"):
    out = []
    for i in range(len(items)):
        if mode == "fast":
            out.append(items[i])
    return out


def parse(text):
    try:
        return int(text)
    except ValueError as exc:
        raise RuntimeError("bad number") from exc
'''

_CORE = '''"""Core helpers."""


def compute_checksum_total(values):
    return sum(values) % 251
'''

_USER_A = """from pkg.alpha.core import compute_checksum_total


def first(values):
    return compute_checksum_total(values)


def second(values):
    total = compute_checksum_total(values[1:])
    return total
"""

_USER_B = """from pkg.alpha import core


def third(values):
    return core.compute_checksum_total(values)
"""


@pytest.fixture(scope="session")
def mini_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small git repository standing in for the clio-agent base commit."""
    repo = tmp_path_factory.mktemp("mini-base") / "repo"
    files: dict[str, str | None] = {
        "src/pkg/__init__.py": "",
        "src/pkg/alpha/__init__.py": '__all__ = ["compute_checksum_total"]\n',
        "src/pkg/alpha/core.py": _CORE,
        "src/pkg/alpha/extra.py": "def helper():\n    return 1\n",
        "src/pkg/beta/__init__.py": "",
        "src/pkg/beta/users.py": _USER_A,
        "src/pkg/gamma/__init__.py": "",
        "src/pkg/gamma/more.py": _USER_B,
        "src/pkg/tools/__init__.py": "",
        "README.md": "mini\n",
    }
    for n in range(1, 7):
        files[f"src/pkg/tools/mod{n}.py"] = _SITES.format(n=n)
    build_history(repo, [("base", files)])
    return repo
