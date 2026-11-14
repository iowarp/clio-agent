"""Record a folder's changes in git, run by CLIO itself: the ``fs_git_commit`` tool.

The agent's own commands cannot change a repository: the OS fence mounts ``.git``
read-only in every folder they may write, and the file tools refuse ``.git`` too
(:meth:`~clio_agent.tools.file_policy.FileAccessPolicy._refuse_git_metadata`). Both
exist because what ``.git`` holds runs later, outside any sandbox: a hook planted
there runs the next time the person commits.

This tool is the sanctioned way in. CLIO runs git in its own process, after the
permission gate has asked the person (the tool is a declared write), and runs it so
that nothing in the repository can execute: hooks are pointed at an empty folder,
fsmonitor is off, automatic maintenance (a detached ``git gc``) is off, and git can
neither open an editor nor ask on a terminal. Only ``init``, ``add`` and ``commit``
change anything, and only inside the folder the person approved.

The commit is made as the person's configured git identity. When git has none, the
tool says so and commits nothing; ``author`` supplies one only for a name and email
the person gave. Nothing is guessed: a refusal comes back as a typed error the model
reads, and git's own message is quoted, never interpreted.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clio_agent.platform_paths import path_is_within
from clio_agent.runtime.git_executable import GitExecutable, GitUnavailableError, resolve_git
from clio_agent.tools.file_policy import (
    FileAccessPolicy,
    _active_workspace_root,
    validate_non_empty_string,
)

#: Most committed files a result lists; the total is always given.
MAX_LISTED_FILES = 200

#: Longest a single git step may run. ``add`` of a large folder hashes every file.
GIT_STEP_TIMEOUT_S = 300.0

#: Most characters of git's own message quoted in an error.
_MAX_GIT_MESSAGE = 2_000

#: ``git diff-tree --name-status`` letters, in words.
_STATUS_WORDS = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}

#: Variables that would point git at another repository, index or configuration.
_CLEARED_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)

_AUTHOR = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>\s*$")


class _GitStepError(Exception):
    """A git step that did not succeed; carries the typed result to return."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result["error"]["message"])
        self.result = result


def _error(code: str, message: str, path: str, next_action: str = "", **details: Any) -> dict:
    """A typed refusal the model reads as the tool result."""

    error: dict[str, Any] = {"type": "git_commit", "code": code, "message": message}
    if next_action:
        error["next_action"] = next_action
    if details:
        error["details"] = details
    return {"ok": False, "path": path, "error": error}


class _Git:
    """One call's git: resolved executable, confined configuration and environment."""

    def __init__(self, git: GitExecutable, cwd: Path, empty_hooks: Path) -> None:
        self.git = git
        self.cwd = cwd
        self.config = (
            "-c",
            f"core.hooksPath={empty_hooks}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "core.editor=true",
        )
        #: ``--git-dir``/``--work-tree`` once the repository is chosen: no later step may
        #: find another one (a sandboxed command's empty ``.git`` placeholder, or none,
        #: would otherwise send git up to an enclosing repository).
        self.pinned: tuple[str, ...] = ()
        env = {k: v for k, v in os.environ.items() if k not in _CLEARED_ENV}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_PAGER": "cat"})
        self.env = git.env(env) or env

    def run(
        self, step: str, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run one git step; a timeout becomes a typed refusal."""

        try:
            return subprocess.run(  # noqa: S603 - argv list, no shell
                self.git.command(*self.config, *self.pinned, *args),
                cwd=self.cwd,
                env={**self.env, **(env or {})},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_STEP_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _GitStepError(
                _error(
                    "git_timed_out",
                    f"git {step} did not finish within {GIT_STEP_TIMEOUT_S:.0f} seconds",
                    str(self.cwd),
                    step=step,
                )
            ) from exc

    def must(
        self, step: str, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run one git step that has to succeed, quoting git's own message if it fails."""

        done = self.run(step, *args, env=env)
        if done.returncode != 0:
            said = (done.stderr or done.stdout).strip()[-_MAX_GIT_MESSAGE:]
            raise _GitStepError(
                _error(
                    "git_failed",
                    f"git {step} failed: {said}" if said else f"git {step} failed",
                    str(self.cwd),
                    step=step,
                    exit_code=done.returncode,
                )
            )
        return done


def _identity(author: str) -> dict[str, str] | None:
    """The environment naming ``author`` as author and committer; None when malformed."""

    match = _AUTHOR.match(author)
    if match is None:
        return None
    name, email = match["name"], match["email"]
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _toplevel(git: _Git) -> Path | None:
    done = git.run("rev-parse", "rev-parse", "--show-toplevel")
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return Path(done.stdout.strip()).resolve()


def _boundary(folder: Path) -> Path:
    """What the person approved: the conversation's workspace, or else the folder itself.

    An enclosing repository is used only when it lies within this. The file policy's
    allowed roots are wider (by default the server's own working directory), and a
    repository there is someone else's project, never this conversation's to commit to.
    """

    workspace = _active_workspace_root()
    return workspace if workspace is not None and path_is_within(folder, workspace) else folder


def _committed_files(git: _Git) -> tuple[list[dict[str, str]], int]:
    """The files the new commit changed, listed up to :data:`MAX_LISTED_FILES`."""

    done = git.must(
        "diff-tree", "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-z", "HEAD"
    )
    fields = done.stdout.split("\0")
    files: list[dict[str, str]] = []
    total = 0
    for index in range(0, len(fields) - 1, 2):
        status, name = fields[index], fields[index + 1]
        if not status:
            continue
        total += 1
        if len(files) < MAX_LISTED_FILES:
            files.append({"path": name, "status": _STATUS_WORDS.get(status[:1], status)})
    return files, total


def git_commit(
    path: str,
    message: str,
    files: Sequence[str] | None = None,
    init: bool = False,
    author: str = "",
) -> dict[str, Any]:
    """Stage and commit changes under ``path`` (or exactly ``files``), creating the repository on ``init``."""

    validate_non_empty_string(path, field="path")
    validate_non_empty_string(message, field="message")
    policy = FileAccessPolicy.from_env()
    folder = policy.validate_write_directory(path, field="path")
    identity: dict[str, str] | None = None
    if author.strip():
        identity = _identity(author)
        if identity is None:
            return _error(
                "invalid_author",
                f"author must look like 'Name <email>', got {author!r}",
                str(folder),
                "Use the name and email the person gave, as 'Name <email>'.",
            )
    try:
        executable = resolve_git()
    except GitUnavailableError as exc:
        return _error("git_unavailable", str(exc), str(folder))
    with tempfile.TemporaryDirectory(prefix="clio-git-no-hooks-") as empty_hooks:
        git = _Git(executable, folder, Path(empty_hooks))
        try:
            return _commit(git, folder, message, files or (), init, identity)
        except _GitStepError as failed:
            return failed.result


def _commit(
    git: _Git,
    folder: Path,
    message: str,
    files: Sequence[str],
    init: bool,
    identity: dict[str, str] | None,
) -> dict[str, Any]:
    if identity is None and git.run("var", "var", "GIT_COMMITTER_IDENT").returncode != 0:
        return _error(
            "identity_unknown",
            "git has no name and email to commit as on this computer",
            str(folder),
            "Ask the person which name and email to commit as, then pass them as author; "
            "never make one up.",
        )
    repository = _toplevel(git)
    if repository is not None and not path_is_within(repository, _boundary(folder)):
        if not init:
            return _error(
                "repository_outside_workspace",
                f"{folder} is inside the repository at {repository}, which is outside "
                "this conversation's workspace",
                str(folder),
                "Tell the person; if they want a separate repository for this folder, call "
                "again with init true.",
                repository=str(repository),
            )
        repository = None  # init: this folder gets a repository of its own
    initialized = False
    if repository is None:
        if not init:
            return _error(
                "not_a_repository",
                f"{folder} is not inside a git repository",
                str(folder),
                "If the person asked for a repository here, call again with init true.",
            )
        git.must("init", "init", "--quiet")
        repository, initialized = folder, True
    git_dir = git.must("rev-parse", "rev-parse", "--absolute-git-dir").stdout.strip()
    git.pinned = ("--git-dir", git_dir, "--work-tree", str(repository))
    pathspecs = [f":(literal){name}" for name in files] or ["."]
    git.must("add", "add", "--all", "--", *pathspecs)
    staged = git.run("diff", "diff", "--cached", "--quiet", "--", *pathspecs)
    base = {"ok": True, "path": str(folder), "repository": str(repository)}
    if staged.returncode == 0:
        return {
            **base,
            "committed": False,
            "initialized": initialized,
            "reason": "nothing_to_commit",
        }
    git.must("commit", "commit", "--quiet", "--message", message, "--", *pathspecs, env=identity)
    commit = git.must("rev-parse", "rev-parse", "HEAD").stdout.strip()
    branch = git.run("symbolic-ref", "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    who = git.must("log", "log", "-1", "--format=%an <%ae>").stdout.strip()
    listed, total = _committed_files(git)
    return {
        **base,
        "committed": True,
        "initialized": initialized,
        "commit": commit,
        "branch": branch,
        "message": message,
        "author": who,
        "files": listed,
        "files_total": total,
        "files_truncated": total > len(listed),
    }


__all__ = ["MAX_LISTED_FILES", "git_commit"]
