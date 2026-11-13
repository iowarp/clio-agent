"""``fs_git_commit``: CLIO records changes in git itself, and nothing in the repository runs.

The agent's commands see ``.git`` read-only and its file tools refuse it, so a request
like "put this folder under git and make an initial commit" had no way to succeed.
This tool is that way, behind the permission prompt: these tests pin what it may and
may not do, and the human presentation of each outcome.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from clio_agent.gact.runtime.grant_resolver import is_read_only
from clio_agent.tools import git_commit as git_commit_module
from clio_agent.tools.catalog import TOOL_CATALOG
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError
from clio_agent.tools.git_commit import git_commit
from clio_agent.tools.tool_presentation import present_mcp_result, starting_presentation

ADA = "Ada Lovelace <ada@example.org>"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated git home with an identity, and the scratch tree as the allowed root."""

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    work = (tmp_path / "work").resolve()
    work.mkdir()
    monkeypatch.setattr(
        FileAccessPolicy, "from_env", classmethod(lambda cls: cls(allowed_roots=(work,)))
    )
    # The test tree may sit inside another checkout: git stops looking above tmp_path.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    return home


@pytest.fixture
def folder(tmp_path: Path, home: Path) -> Path:
    root = (tmp_path / "work" / "survey").resolve()
    (root / "data").mkdir(parents=True)
    (root / "notes.md").write_text("# Survey\n", encoding="utf-8")
    (root / "data" / "counts.csv").write_text("site,count\nA,3\n", encoding="utf-8")
    return root


def _structured(row: dict) -> dict:
    return {"structuredContent": row, "content": [{"type": "text", "text": str(row)}]}


def test_init_creates_the_repository_and_commits_everything(folder: Path) -> None:
    result = git_commit(str(folder), "Initial commit", init=True)

    assert result["ok"] is True and result["committed"] is True and result["initialized"] is True
    assert result["branch"] == "main"
    assert result["author"] == ADA
    assert {(f["path"], f["status"]) for f in result["files"]} == {
        ("notes.md", "added"),
        ("data/counts.csv", "added"),
    }
    assert _git(folder, "log", "--format=%s") == "Initial commit"
    assert _git(folder, "rev-parse", "HEAD") == result["commit"]


def test_without_init_a_plain_folder_is_not_made_a_repository(folder: Path) -> None:
    result = git_commit(str(folder), "Initial commit")

    assert result["ok"] is False
    assert result["error"]["code"] == "not_a_repository"
    assert "init true" in result["error"]["next_action"]
    assert not (folder / ".git").exists()


def test_no_hook_or_fsmonitor_in_the_repository_runs(folder: Path, tmp_path: Path) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    ran = tmp_path / "ran"
    hook = f"#!/bin/sh\necho $0 >> {ran}\n"
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
        script = folder / ".git" / "hooks" / name
        script.write_text(hook, encoding="utf-8")
        script.chmod(0o755)
    elsewhere = tmp_path / "hooks"
    elsewhere.mkdir()
    (elsewhere / "pre-commit").write_text(hook, encoding="utf-8")
    (elsewhere / "pre-commit").chmod(0o755)
    _git(folder, "config", "core.hooksPath", str(elsewhere))
    _git(folder, "config", "core.fsmonitor", f"sh -c 'echo fsmonitor >> {ran}'")
    (folder / "notes.md").write_text("# Survey\n\nSite B added.\n", encoding="utf-8")

    result = git_commit(str(folder), "Add site B")

    assert result["committed"] is True
    assert not ran.exists(), ran.read_text(encoding="utf-8")


def test_named_files_are_committed_and_the_persons_other_staged_work_is_left(
    folder: Path,
) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    (folder / "notes.md").write_text("# Survey\n\nRevised.\n", encoding="utf-8")
    (folder / "data" / "counts.csv").write_text("site,count\nA,4\n", encoding="utf-8")
    _git(folder, "add", "data/counts.csv")  # the person's own staged change
    (folder / "draft.txt").write_text("not yet\n", encoding="utf-8")

    result = git_commit(str(folder), "Revise notes", files=["notes.md"])

    assert [(f["path"], f["status"]) for f in result["files"]] == [("notes.md", "modified")]
    assert _git(folder, "diff", "--cached", "--name-only") == "data/counts.csv"
    assert "?? draft.txt" in _git(folder, "status", "--short")


def test_a_deleted_file_is_recorded_as_deleted(folder: Path) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    (folder / "data" / "counts.csv").unlink()
    (folder / "notes.md").write_text("# Survey\n\nCounts dropped.\n", encoding="utf-8")

    result = git_commit(str(folder), "Drop counts")

    assert sorted((f["path"], f["status"]) for f in result["files"]) == [
        ("data/counts.csv", "deleted"),
        ("notes.md", "modified"),
    ]
    view = present_mcp_result("fs_git_commit", {"path": str(folder)}, _structured(result))
    lines = [b.get("text") or b.get("label") for b in view["blocks"] if b["id"] != "folder"]
    assert lines[-4:] == ["1 modified", "notes.md", "1 deleted", "data/counts.csv"]


def test_nothing_to_commit_is_said_and_makes_no_commit(folder: Path) -> None:
    git_commit(str(folder), "Initial commit", init=True)

    result = git_commit(str(folder), "Again")

    assert result == {
        "ok": True,
        "path": str(folder),
        "repository": str(folder),
        "committed": False,
        "initialized": False,
        "reason": "nothing_to_commit",
    }
    assert _git(folder, "rev-list", "--count", "HEAD") == "1"


def test_without_an_identity_nothing_is_created_or_guessed(
    folder: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".gitconfig").write_text("", encoding="utf-8")
    monkeypatch.setenv("EMAIL", "")
    monkeypatch.setattr("socket.getfqdn", lambda *_: "(none)", raising=False)

    refused = git_commit(str(folder), "Initial commit", init=True)
    if refused["ok"]:
        pytest.skip("this machine's git derives an identity from the host name")
    assert refused["error"]["code"] == "identity_unknown"
    assert "never make one up" in refused["error"]["next_action"]
    assert not (folder / ".git").exists()

    given = git_commit(str(folder), "Initial commit", init=True, author="Grace <grace@example.org>")
    assert given["author"] == "Grace <grace@example.org>"
    assert _git(folder, "log", "--format=%cn <%ce>") == "Grace <grace@example.org>"


def test_a_malformed_author_is_refused(folder: Path) -> None:
    result = git_commit(str(folder), "Initial commit", init=True, author="Grace")
    assert result["error"]["code"] == "invalid_author"
    assert not (folder / ".git").exists()


def test_a_folder_outside_the_allowed_roots_is_refused(folder: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(FilePolicyError) as caught:
        git_commit(str(outside), "Initial commit", init=True)
    assert caught.value.code == "outside_allowed_roots"
    assert not (outside / ".git").exists()


def test_the_git_folder_itself_is_never_the_target(folder: Path) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    with pytest.raises(FilePolicyError) as caught:
        git_commit(str(folder / ".git"), "x")
    assert caught.value.code == "git_metadata_protected"


def test_an_enclosing_repository_outside_the_roots_gets_a_repository_of_its_own(
    folder: Path, tmp_path: Path
) -> None:
    _git(tmp_path, "init", "-q")  # e.g. a home directory kept in git

    without_init = git_commit(str(folder), "Initial commit")
    assert without_init["error"]["code"] == "repository_outside_workspace"
    assert without_init["error"]["details"] == {"repository": str(tmp_path.resolve())}
    assert "init true" in without_init["error"]["next_action"]
    result = git_commit(str(folder), "Initial commit", init=True)

    assert result["initialized"] is True and result["repository"] == str(folder)
    assert "?? work/" in _git(tmp_path, "status", "--porcelain").splitlines()
    assert _git(tmp_path, "rev-list", "--all") == ""  # nothing went into the outer one


def test_an_enclosing_repository_the_policy_allows_is_still_not_this_workspaces(
    folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live 2026-11-13: the server's allowed roots held its own checkout, which encloses
    the workspace; the commit went to that checkout's repository (only its .gitignore
    stopped it). The workspace is the boundary, not the policy's allowed roots."""

    project = (tmp_path / "work").resolve()
    _git(project, "init", "-q")
    (project / "theirs.txt").write_text("someone else's project\n", encoding="utf-8")
    assert FileAccessPolicy.from_env().validate_write_directory(str(project)) == project

    with tool_workspace_context(folder):
        refused = git_commit(str(folder), "Initial commit")
        made = git_commit(str(folder), "Initial commit", init=True)

    assert refused["error"]["code"] == "repository_outside_workspace"
    assert made["repository"] == str(folder) and made["initialized"] is True
    assert {f["path"] for f in made["files"]} == {"notes.md", "data/counts.csv"}
    assert _git(project, "rev-list", "--all") == ""
    assert "?? theirs.txt" in _git(project, "status", "--porcelain").splitlines()


def test_a_subfolder_of_a_workspace_repository_commits_to_the_workspaces_repository(
    folder: Path,
) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    (folder / "data" / "counts.csv").write_text("site,count\nA,5\n", encoding="utf-8")

    with tool_workspace_context(folder):
        result = git_commit(str(folder / "data"), "Update counts", init=True)

    assert result["repository"] == str(folder) and result["initialized"] is False
    assert not (folder / "data" / ".git").exists()
    assert [f["path"] for f in result["files"]] == ["data/counts.csv"]


def test_an_empty_sandbox_placeholder_git_folder_gets_the_new_repository(folder: Path) -> None:
    """While a sandboxed command runs here, codex holds an empty .git placeholder on
    disk. git does not treat it as a repository; init fills it, and every later step is
    pinned to it rather than found again."""

    (folder / ".git").mkdir()

    result = git_commit(str(folder), "Initial commit", init=True)

    assert result["initialized"] is True and result["repository"] == str(folder)
    assert _git(folder, "log", "--format=%s") == "Initial commit"


def test_a_large_commit_lists_a_bounded_number_of_files(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_commit_module, "MAX_LISTED_FILES", 5)
    for index in range(12):
        (folder / "data" / f"run_{index:02d}_with_a_rather_long_descriptive_name.csv").write_text(
            f"{index}\n", encoding="utf-8"
        )

    result = git_commit(str(folder), "Add runs", init=True)

    assert result["files_total"] == 14 and len(result["files"]) == 5
    assert result["files_truncated"] is True
    view = present_mcp_result("fs_git_commit", {"path": str(folder)}, _structured(result))
    assert view["summary"].startswith("14 files, ")
    assert "Showing 5 of 14 committed files." in [b.get("text") for b in view["blocks"]]


def test_it_is_a_write_the_gate_asks_about_and_the_gateway_serves() -> None:
    from clio_agent.tools.builtin_gateway import get_gateway

    assert "write" in TOOL_CATALOG["fs_git_commit"].tags
    assert not is_read_only("tool", "fs_git_commit", {}, None)
    tools = asyncio.run(get_gateway().list_tools())
    listed = next(tool for tool in tools if tool.name == "fs_git_commit")
    assert listed.annotations.read_only_hint is False
    assert listed.annotations.open_world_hint is False
    assert set(listed.parameters["required"]) == {"path", "message"}


# Presentation showcase: what the person sees for each outcome.


def test_presentation_of_a_new_repositorys_first_commit(folder: Path) -> None:
    result = git_commit(str(folder), "Initial commit of the field survey", init=True)

    view = present_mcp_result("fs_git_commit", {"path": str(folder)}, _structured(result))

    assert view["action"] == "Commit" and view["subject"] == "folder"
    assert view["summary"] == f"2 files, {result['commit'][:7]} on main"
    blocks = {block["id"]: block for block in view["blocks"]}
    assert blocks["folder"]["label"] == "survey"
    assert blocks["initialized"]["text"].startswith("Created a new git repository")
    assert blocks["message"]["text"] == "Initial commit of the field survey"
    assert blocks["author"]["text"] == ADA
    files = sorted((b["label"], b["detail"]) for b in view["blocks"] if b["id"].startswith("file-"))
    assert files == [("data/counts.csv", "added"), ("notes.md", "added")]
    assert all(Path(b["uri"]).is_file() for b in view["blocks"] if b["id"].startswith("file-"))


def test_presentation_of_nothing_to_commit(folder: Path) -> None:
    git_commit(str(folder), "Initial commit", init=True)
    result = git_commit(str(folder), "Again")

    view = present_mcp_result("fs_git_commit", {"path": str(folder)}, _structured(result))

    assert view["summary"] == "No changes to commit"
    assert "status" not in view
    assert [block["id"] for block in view["blocks"]] == ["folder"]


def test_presentation_of_a_refusal_keeps_the_reason_and_next_step(folder: Path) -> None:
    result = git_commit(str(folder), "Initial commit")

    view = present_mcp_result("fs_git_commit", {"path": str(folder)}, _structured(result))

    assert view["status"] == "failed"
    texts = [block.get("text", "") for block in view["blocks"]]
    assert any("is not inside a git repository" in text for text in texts)
    assert any("call again with init true" in text for text in texts)
    assert view["blocks"][0]["id"] == "folder"


def test_presentation_while_git_runs_names_the_folder_and_message() -> None:
    view = starting_presentation(
        "fs_git_commit", {"path": "/work/survey", "message": "Initial commit"}
    )

    assert view is not None
    assert view["action"] == "Commit"
    assert [block["id"] for block in view["blocks"]] == ["folder", "message"]
    assert "summary" not in view or not view["summary"]
