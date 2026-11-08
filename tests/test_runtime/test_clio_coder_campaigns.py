"""Arena matches and research campaigns delegated to Clio Coder, against a scripted fake CLI.

The fake speaks the two real commands' JSON (``arena run --json``, ``research start --json``) and,
like the real research loop, ratchets a commit onto ``clio/research/<id>`` in the scratch clone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from clio_agent.runtime.clio_coder import ClioCoderError, ClioCoderInstall, InstanceHome
from clio_agent.runtime.clio_coder.campaigns import (
    ArenaRequest,
    ResearchRequest,
    adopt_arena_entry,
    discard_campaign,
    prepare_campaign_clone,
    run_arena,
    run_research,
)

DIFF = (
    "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n-    return a - b\n+    return a + b\n"
)

FAKE_CLIO_CODER = textwrap.dedent(
    """
    import json, subprocess, sys
    from pathlib import Path

    args = sys.argv[1:]

    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()

    if args[:2] == ["research", "start"]:
        base = git("rev-parse", "HEAD")
        best = base
        if "--goal" not in args:
            Path("calc.py").write_text("def add(a, b):\\n    return a + b\\n")
            git("add", "calc.py")
            tree = git("write-tree")
            best = git("-c", "user.name=c", "-c", "user.email=c@c", "commit-tree", tree,
                       "-p", base, "-m", "research r1 it-0001")
            git("update-ref", "refs/heads/clio/research/r1", best)
            git("checkout", "-q", "--", ".")
            git("reset", "-q")
        print(json.dumps({
            "id": "r1", "state": "stopped", "best": best, "ratchets": int(best != base),
            "stop": {"reason": "max-iterations"}, "spend": {"usd": 0},
            "aa": {"grade": {"verdict": "unchanged"}},
            "iterations": [{
                "iteration": 1, "outcome": "ratcheted" if best != base else "no-op",
                "detail": "", "changed": ["calc.py"],
                "grade": {"baseMedian": 10, "candidateMedian": 4, "verdict": "improved"},
                "gates": [{"check": "tests", "passed": True}],
            }],
        }))
    elif args[:2] == ["arena", "run"]:
        entry = {"status": "done", "exitCode": 0, "timedOut": False, "toolCalls": 3,
                 "toolErrors": 0, "diffStat": {"files": 1}, "check": {"id": "t", "exitCode": 0},
                 "finalText": "fixed", "diff": %r, "diffTruncated": False, "error": None,
                 "reveal": {"model": "secret"}}
        print(json.dumps({"id": "m1", "status": "complete", "error": None, "check": "t",
                          "isolation": "worktree",
                          "entries": [entry | {"label": "A"},
                                      entry | {"label": "B", "diffTruncated": True}]}))
    else:
        sys.exit(2)
    """
    % DIFF
)


@pytest.fixture
def install(tmp_path: Path) -> ClioCoderInstall:
    script = tmp_path / "fake_clio_coder.py"
    script.write_text(FAKE_CLIO_CODER, "utf-8")
    return ClioCoderInstall((sys.executable, "-B", str(script)))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n", "utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "calc.py"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_clone_takes_head_and_the_uncommitted_catalog_but_never_the_working_tree(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "calc.py").write_text("dirty\n", "utf-8")
    catalog = repo / ".clio-coder" / "verifiers.yaml"
    catalog.parent.mkdir()
    catalog.write_text("version: 2\nchecks: []\n", "utf-8")

    workspace = prepare_campaign_clone(repo, tmp_path / "state", "run1")

    assert workspace.clone == tmp_path / "state" / "run1" / "repo"
    assert (workspace.clone / "calc.py").read_text("utf-8") == "def add(a, b):\n    return a - b\n"
    assert (workspace.clone / ".clio-coder" / "verifiers.yaml").read_text("utf-8") == (
        catalog.read_text("utf-8")
    )
    assert "__pycache__/" in (workspace.clone / ".git" / "info" / "exclude").read_text("utf-8")
    assert (repo / "calc.py").read_text("utf-8") == "dirty\n"
    with pytest.raises(ClioCoderError):
        discard_campaign(workspace, tmp_path / "elsewhere")
    discard_campaign(workspace, tmp_path / "state")
    assert not workspace.territory.exists()


def test_research_brings_a_ratcheted_best_back_as_a_branch_only(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    home = InstanceHome(tmp_path / "state" / "home")
    home.prepare(None)
    base = _git(repo, "rev-parse", "HEAD")
    request = ResearchRequest(check="bench", metric="ops", direction="min", max_iterations=1)

    workspace = prepare_campaign_clone(repo, tmp_path / "state" / "research", "c1")
    result = asyncio.run(run_research(install, home, workspace, request))

    assert result["branch"] == "clio-coder/research-r1"
    assert result["best_metric"] == 4
    assert result["iterations"][0]["gates"] == [{"check": "tests", "passed": True}]
    assert _git(repo, "rev-parse", "clio-coder/research-r1") == result["commit"] != base
    assert "return a + b" in _git(repo, "show", "clio-coder/research-r1:calc.py")
    assert _git(repo, "rev-parse", "HEAD") == base
    assert _git(repo, "status", "--porcelain") == ""

    unchanged = prepare_campaign_clone(repo, tmp_path / "state" / "research", "c2")
    noop = ResearchRequest(
        check="bench", metric="ops", direction="min", max_iterations=1, goal="nothing"
    )
    result = asyncio.run(run_research(install, home, unchanged, noop))
    assert result["branch"] is None and result["best_metric"] is None


def test_arena_view_stays_blind_and_adoption_commits_one_entry(
    repo: Path, tmp_path: Path, install: ClioCoderInstall
) -> None:
    with pytest.raises(ValueError):
        ArenaRequest(prompt="x", contenders=["t/m"]).args()
    home = InstanceHome(tmp_path / "state" / "home")
    home.prepare(None)
    workspace = prepare_campaign_clone(repo, tmp_path / "state" / "arena", "m")
    request = ArenaRequest(prompt="fix add", contenders=["t/m1", "t/m2"], check="t")

    match = asyncio.run(run_arena(install, home, workspace, request))

    assert [entry["label"] for entry in match["entries"]] == ["A", "B"]
    assert "secret" not in json.dumps(match["entries"])
    assert match["entries"][0]["check"] == {"id": "t", "passed": True}
    adopted = adopt_arena_entry(workspace, match, "A", tmp_path / "state" / "worktrees")
    assert adopted["branch"] == "clio-coder/arena-m1-A"
    assert "return a + b" in _git(repo, "show", "clio-coder/arena-m1-A:calc.py")
    assert len(_git(repo, "worktree", "list").splitlines()) == 1
    with pytest.raises(ClioCoderError, match="truncated"):
        adopt_arena_entry(workspace, match, "B", tmp_path / "state" / "worktrees")
