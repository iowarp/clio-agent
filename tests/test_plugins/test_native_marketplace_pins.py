"""A native marketplace lists a package kept in another repository, pinned by commit."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.plugins.marketplace import read_native_marketplace


def _index(root: Path, plugins: list[dict]) -> None:
    index = root / "ai.iowarp.clio" / "marketplace.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"name": "clio", "plugins": plugins}), encoding="utf-8")


def test_a_pinned_git_package_is_listed_beside_the_local_plugins(tmp_path: Path) -> None:
    _index(
        tmp_path,
        [
            {
                "name": "wtfp",
                "source": {
                    "source": "git-subdir",
                    "url": "https://github.com/JaimeCernuda/wtf-p",
                    "path": "clio-plugin",
                    "sha": "0123456789abcdef0123456789abcdef01234567",
                },
            },
            {"name": "floating", "source": {"source": "github", "repo": "o/r"}},
            {"name": "odd", "source": {"source": "svn"}},
        ],
    )
    contents = read_native_marketplace(tmp_path, trusted=False)
    assert [(p.name, p.url, p.path) for p in contents.git_plugins] == [
        ("wtfp", "https://github.com/JaimeCernuda/wtf-p", "clio-plugin")
    ]
    # Never followed silently: an unpinned or unknown source says why.
    assert [(d.code, d.plugin) for d in contents.diagnostics] == [
        ("plugin_source_unpinned", "floating"),
        ("plugin_source_unsupported", "odd"),
    ]


def test_an_index_without_plugins_lists_only_the_plugins_folder(tmp_path: Path) -> None:
    _index(tmp_path, [])
    assert read_native_marketplace(tmp_path, trusted=False).git_plugins == []
