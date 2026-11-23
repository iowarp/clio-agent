"""Flows read from source without importing DSPy, and the archive unpack's filter.

A flows module is read statically only when its records are provably the import's
own; every other module is imported as before. The unpack skips the per-member
``realpath`` only where ``tarfile.data_filter``'s containment check holds by
construction, and gives the same attributes.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from clio_agent.plugins import loader, sources
from clio_agent.plugins.commands import BUILTIN_ROOT
from tests.test_plugins.helpers import plugin

REPO = Path(__file__).resolve().parents[2]
FLOW_IMPORT = "from clio_agent.sdk import Flow\n"


def _flow_roots() -> list[Path]:
    bases = [BUILTIN_ROOT, REPO / "external/clio-agent-marketplace"]
    return sorted(
        {
            path.parents[2]
            for base in bases
            if base.is_dir()
            for path in base.rglob("ai.iowarp.clio/flows/*.py")
        }
    )


def _both(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[loader._PythonParts, ...]:
    """The parts read with the static path, then with every flows module imported."""
    static = loader._python_parts(root, root.name, {})
    with monkeypatch.context() as patched:
        patched.setattr(loader, "_static_flows", lambda *_args: None)
        imported = loader._python_parts(root, root.name, {})
    return static, imported


def _same(static: loader._PythonParts, imported: loader._PythonParts) -> None:
    assert static.flows == imported.flows
    assert [(d.code, d.message) for d in static.diagnostics] == [
        (d.code, d.message) for d in imported.diagnostics
    ]


def _flows_plugin(tmp_path: Path, source: str, name: str = "flowy") -> Path:
    return plugin(tmp_path, name, files={"ai.iowarp.clio/flows/f.py": source})


@pytest.mark.parametrize("root", _flow_roots(), ids=lambda root: root.name)
# The import side of the comparison loads DSPy, which alone can take most of 30 s cold.
@pytest.mark.timeout(120)
def test_shipped_flow_modules_read_statically_as_the_import_reads_them(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clio = root / "ai.iowarp.clio"
    for path in sorted((clio / "flows").glob("*.py")):
        relative = path.relative_to(clio).as_posix()
        assert loader._static_flows(path, relative, root.name) is not None, relative
    static, imported = _both(root, monkeypatch)
    assert imported.flows
    _same(static, imported)


def test_loading_flows_imports_neither_the_module_nor_dspy(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = (
        "import pathlib\n"
        + FLOW_IMPORT
        + f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        + "class Tally(Flow):\n    id = 'tally'\n    shape = 'railroad'\n"
    )
    root = _flows_plugin(tmp_path, source)
    probe = (
        "import json, sys\nfrom pathlib import Path\nfrom clio_agent.plugins import loader\n"
        "root = Path(sys.argv[1])\nparts = loader._python_parts(root, root.name, {})\n"
        "print(json.dumps({'flows': [f.id for f in parts.flows],"
        " 'dspy': 'dspy' in sys.modules, 'flow': 'clio_agent.sdk.flow' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(root)],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "flows": ["tally"],
        "dspy": False,
        "flow": False,
    }
    assert not marker.exists()


def test_reading_shipped_agent_specs_does_not_import_dspy() -> None:
    market = REPO / "external/clio-agent-marketplace"
    if not (market / "agents").is_dir():
        pytest.skip("the marketplace submodule is not checked out")
    probe = (
        "import json, sys\nfrom pathlib import Path\n"
        "from clio_agent.plugins.marketplace import load_agent_specs\n"
        "specs, diags = load_agent_specs(Path(sys.argv[1]), 'm')\n"
        "print(json.dumps({'specs': len(specs), 'diags': len(diags),"
        " 'dspy': 'dspy' in sys.modules, 'flow': 'clio_agent.sdk.flow' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(market)],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
    )
    row = json.loads(done.stdout.strip().splitlines()[-1])
    assert row["specs"] > 0
    assert (row["diags"], row["dspy"], row["flow"]) == (0, False, False)


def test_flow_selection_still_reads_flow_values() -> None:
    from clio_agent.sdk.agent import _flow_selection
    from clio_agent.sdk.flow import Flow

    class Picked(Flow):
        id = "picked"
        description = "d"

    assert _flow_selection(Picked) == {"id": "picked", "options": {}}
    assert _flow_selection(" react ") == {"id": "react", "options": {}}


STATIC_CASES = {
    "inherited-id-and-docstring": (
        FLOW_IMPORT
        + "class Base(Flow):\n    '''Base flow.'''\n    id = 'base'\n    description = 'd'\n\n"
        + "class Child(Base):\n    '''\n        Indented\n          doc.\n    '''\n"
        + "    shape: str = 'railroad-beta'\n"
    ),
    "private-and-no-id": (
        FLOW_IMPORT
        + "class _Hidden(Flow):\n    id = 'hidden'\n\n"
        + "class Nameless(Flow):\n    '''No id.'''\n\n"
        + "class Shown(_Hidden):\n    pass\n"
    ),
    "external-calls-and-helpers": (
        "import re\nimport dspy\n"
        + FLOW_IMPORT
        + "PATTERN = re.compile('x')\n\n"
        + "class Ask(dspy.Signature):\n    '''Ask.'''\n    q: str = dspy.InputField()\n\n"
        + "def helper(flow):\n    return Tally, flow\n\n"
        + "class Tally(Flow):\n    id = 'tally'\n    description = 'counts'\n"
        + "    @property\n    def size(self):\n        return Tally\n"
    ),
}


@pytest.mark.parametrize("source", STATIC_CASES.values(), ids=STATIC_CASES.keys())
def test_plain_flow_modules_read_statically(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _flows_plugin(tmp_path, source)
    assert loader._static_flows(root / "ai.iowarp.clio/flows/f.py", "flows/f.py", "flowy")
    static, imported = _both(root, monkeypatch)
    _same(static, imported)


def test_a_flow_without_an_id_is_reported_statically(tmp_path: Path) -> None:
    root = _flows_plugin(tmp_path, FLOW_IMPORT + "class Nameless(Flow):\n    pass\n")
    parts = loader._static_flows(root / "ai.iowarp.clio/flows/f.py", "flows/f.py", "flowy")
    assert parts is not None and parts.flows == []
    assert [(d.code, d.message) for d in parts.diagnostics] == [
        ("flow_invalid", "flows/f.py:Nameless has no id")
    ]


TALLY = FLOW_IMPORT + "class Tally(Flow):\n    id = 'tally'\n"
IMPORTED_CASES = {
    "alias": TALLY + "Alias = Tally\n",
    "decorated": "def keep(cls):\n    return cls\n\n"
    + FLOW_IMPORT
    + "@keep\nclass Tally(Flow):\n    id = 'tally'\n",
    "module-base": "import clio_agent.sdk.flow as engine\n\n"
    "class Tally(engine.Flow):\n    id = 'tally'\n",
    "globals": TALLY + "globals()['Again'] = Tally\n",
    "type-call": FLOW_IMPORT + "Made = type('Made', (Flow,), {'id': 'made'})\n",
    "factory": FLOW_IMPORT
    + "def make():\n    return type('Made', (Flow,), {'id': 'made'})\n\nMade = make()\n",
    "conditional": FLOW_IMPORT + "if True:\n    class Tally(Flow):\n        id = 'tally'\n",
    "field-set-later": TALLY + "Tally.id = 'renamed'\n",
    "field-set-in-function": TALLY
    + "def rename(cls):\n    cls.description = 'late'\n\nrename(object)\n",
    "module-renamed": FLOW_IMPORT + "class Tally(Flow):\n    id = 'tally'\n    __module__ = 'x'\n",
    "computed-id": FLOW_IMPORT + "class Tally(Flow):\n    id = 'ta' + 'lly'\n",
    "walrus-id": FLOW_IMPORT + "class Tally(Flow):\n    x = (id := 'tally')\n",
    "rebound": TALLY + "Tally = None\n",
    "global-in-function": TALLY + "def g():\n    global Tally\n    Tally = None\n",
    "syntax-error": "def oops(:\n",
    "raises": "raise RuntimeError('no')\n",
}


@pytest.mark.parametrize("source", IMPORTED_CASES.values(), ids=IMPORTED_CASES.keys())
def test_anything_not_plainly_stated_is_imported(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _flows_plugin(tmp_path, source)
    path = root / "ai.iowarp.clio/flows/f.py"
    if source.startswith("raise"):
        # Nothing it states is a flow: read as none, the module left to its run.
        parts = loader._static_flows(path, "flows/f.py", "flowy")
        assert parts is not None and parts.flows == [] and parts.diagnostics == []
        return
    assert loader._static_flows(path, "flows/f.py", "flowy") is None
    static, imported = _both(root, monkeypatch)
    _same(static, imported)


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        (tarfile.REGTYPE, 0o777),
        (tarfile.REGTYPE, 0o644),
        (tarfile.REGTYPE, 0o4755),
        (tarfile.REGTYPE, 0o100),
        (tarfile.REGTYPE, 0o000),
        (tarfile.REGTYPE, 0o666),
        (tarfile.AREGTYPE, 0o751),
        (tarfile.DIRTYPE, 0o2775),
        (tarfile.DIRTYPE, 0o700),
    ],
)
def test_the_fast_filter_gives_the_data_filters_attributes(
    kind: bytes, mode: int, tmp_path: Path
) -> None:
    member = tarfile.TarInfo("pkg/sub/file.txt")
    member.type = kind
    member.mode = mode
    member.uid, member.gid, member.uname, member.gname = 1000, 1000, "someone", "staff"
    member.mtime = 1_700_000_000
    fast = sources._data_view(member)
    slow = tarfile.data_filter(member, str(tmp_path))
    for attribute in ("name", "mode", "uid", "gid", "uname", "gname", "mtime", "type"):
        assert getattr(fast, attribute) == getattr(slow, attribute), attribute


@pytest.mark.parametrize("name", ["a", "a/b.txt", "deep/er/file.tar.gz", ".hidden/x"])
def test_plain_names(name: str) -> None:
    assert sources._plain_name(name)


@pytest.mark.parametrize(
    "name", ["", "../x", "a/../b", "/abs", "a/./b", "a//b", "a/", ".", "..", "./a"]
)
def test_names_resolved_by_the_data_filter(name: str) -> None:
    assert not sources._plain_name(name)


@pytest.mark.skipif(os.name != "nt", reason="Windows path rules")
@pytest.mark.parametrize("name", ["C:x", "a\\b", "CON", "dir/NUL.txt", "a:b", "x.", "x "])
def test_windows_names_resolved_by_the_data_filter(name: str) -> None:
    assert not sources._plain_name(name)


def _bundle(*members: tuple[str, bytes, str | bytes]) -> tarfile.TarFile:
    """A tarball under one top folder: ``(name, type, data-or-linkname)`` members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        top = tarfile.TarInfo("r-top/")
        top.type = tarfile.DIRTYPE
        bundle.addfile(top)
        for name, kind, payload in members:
            info = tarfile.TarInfo(f"r-top/{name}")
            info.type = kind
            info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            if kind == tarfile.REGTYPE:
                assert isinstance(payload, bytes)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            else:
                if kind == tarfile.SYMTYPE:
                    assert isinstance(payload, str)
                    info.linkname = payload
                bundle.addfile(info)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r")


def _reference_unpack(bundle: tarfile.TarFile, staging: Path) -> None:
    """The unpack before the fast path: every member through ``tarfile.data_filter``."""
    files: list[tuple[tarfile.TarInfo, tarfile.TarInfo]] = []
    rest: list[tarfile.TarInfo] = []
    for member in sources._inner_members(bundle):
        if member.isreg():
            files.append((member, tarfile.data_filter(member, str(staging))))
        else:
            rest.append(member)
    for member, filtered in files:
        path = os.path.join(staging, filtered.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        source = bundle.extractfile(member)
        with open(path, "wb") as out:
            out.write(source.read() if source is not None else b"")
        if filtered.mode is not None:
            os.chmod(path, filtered.mode)
        if filtered.mtime is not None:
            os.utime(path, (filtered.mtime, filtered.mtime))
    bundle.extractall(staging, members=rest, filter="data")


def _outcome(unpack, members, root: Path) -> tuple[str, list[tuple[str, str, str]]]:
    """What ``unpack`` leaves anywhere under ``root``: the refusal type and the tree."""
    staging = root / "stage" / "inner"
    staging.mkdir(parents=True)
    try:
        unpack(_bundle(*members), staging)
        refusal = ""
    except (tarfile.TarError, OSError) as exc:
        refusal = type(exc).__name__
    tree: list[tuple[str, str, str]] = []
    for folder, dirs, names in os.walk(root):
        for name in sorted(dirs + names):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                tree.append((rel, "link", os.readlink(path)))
            elif path.is_dir():
                tree.append((rel, "dir", ""))
            else:
                tree.append((rel, "file", f"{path.read_bytes()!r} {path.stat().st_mode & 0o777:o}"))
    return refusal, sorted(tree)


_HOSTILE = {
    # ``a`` points at ``c/d/../..`` while ``c`` is missing, then ``c`` becomes ``.``.
    "chain-folder": (
        ("a", tarfile.SYMTYPE, "c/d/../.."),
        ("c", tarfile.SYMTYPE, "."),
        ("a/evil", tarfile.DIRTYPE, b""),
    ),
    "chain-file": (
        ("a", tarfile.SYMTYPE, "c/d/../.."),
        ("c", tarfile.SYMTYPE, "."),
        ("a/evil", tarfile.REGTYPE, b"x"),
    ),
    "outward-link": (("up", tarfile.SYMTYPE, "../.."),),
    "outward-link-then-folder": (
        ("up", tarfile.SYMTYPE, ".."),
        ("up/evil", tarfile.DIRTYPE, b""),
    ),
    "absolute-link": (("abs", tarfile.SYMTYPE, "/etc"),),
    "inner-link-then-members": (
        ("real", tarfile.DIRTYPE, b""),
        ("alias", tarfile.SYMTYPE, "real"),
        ("alias/f.txt", tarfile.REGTYPE, b"f"),
        ("alias/sub", tarfile.DIRTYPE, b""),
    ),
    "folder-then-link-of-its-name": (
        ("same", tarfile.DIRTYPE, b""),
        ("same", tarfile.SYMTYPE, ".."),
        ("same/evil", tarfile.DIRTYPE, b""),
    ),
    "dotted-names": (
        ("ok/../../evil", tarfile.REGTYPE, b"x"),
        ("./dot.txt", tarfile.REGTYPE, b"d"),
        ("x/./y", tarfile.DIRTYPE, b""),
    ),
    "plain": (
        ("pkg", tarfile.DIRTYPE, b""),
        ("pkg/run.sh", tarfile.REGTYPE, b"#!/bin/sh\n"),
        ("pkg/deep/a.txt", tarfile.REGTYPE, b"a"),
    ),
}


@pytest.mark.parametrize("case", sorted(_HOSTILE))
def test_the_unpack_refuses_and_writes_exactly_what_the_data_filter_did(
    case: str, tmp_path: Path
) -> None:
    members = _HOSTILE[case]
    before = _outcome(_reference_unpack, members, tmp_path / "reference")
    after = _outcome(sources._unpack, members, tmp_path / "fast")
    assert after == before
    assert all(rel.startswith("stage/inner") or rel == "stage" for rel, _, _ in after[1])


def test_files_folders_and_links_unpack_as_before(tmp_path: Path) -> None:
    staging = tmp_path / "s"
    staging.mkdir()
    bundle = _bundle(
        ("pkg", tarfile.DIRTYPE, b""),
        ("pkg/run.sh", tarfile.REGTYPE, b"#!/bin/sh\n"),
        ("pkg/deep/a.txt", tarfile.REGTYPE, b"a"),
        ("pkg/later", tarfile.DIRTYPE, b""),
    )
    sources._unpack(bundle, staging)
    assert (staging / "pkg/run.sh").read_bytes() == b"#!/bin/sh\n"
    assert (staging / "pkg/deep/a.txt").read_bytes() == b"a"
    assert (staging / "pkg/later").is_dir()
    # data_filter's own refusal: AbsoluteLinkError on POSIX, outside-destination on Windows.
    with pytest.raises(tarfile.FilterError):
        sources._unpack(_bundle(("out", tarfile.SYMTYPE, "/etc")), tmp_path / "t")
