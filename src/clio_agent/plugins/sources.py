"""Fetch plugin sources: local folders and git repositories pinned by commit.

A source is a local path, a git URL (``https://...git``, ``github:owner/repo``)
with an optional ref, or a git URL pinned to a commit. Pinned fetches are
content-addressed by ``(url, commit)`` so two marketplace entries that point
into the same commit (federated ``git-subdir`` entries) share one checkout.

Every value that reaches ``git`` comes from a marketplace file someone else wrote, so
none of it may be read as an option: a commit is hex, a ref passes
``git check-ref-format``, positional arguments follow ``--``, and a source a
marketplace *declares* (``declared=True``) must be a network URL. It never names a
folder on this machine, and ``file``/``ext`` transports are refused.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SourceError(RuntimeError):
    """A source could not be fetched. The message names the source and the cause."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedSource:
    path: Path
    url: str
    commit: str
    local: bool
    #: Why ``commit`` is empty for a local source (typed, never silent).
    commit_reason: str = ""


def normalize_git_url(value: str) -> str:
    text = value.strip()
    if text.startswith("github:"):
        return f"https://github.com/{text.removeprefix('github:').strip('/')}.git"
    if text.count("/") == 1 and not text.startswith((".", "/", "~")) and ":" not in text:
        return f"https://github.com/{text}.git"
    return text


def is_local(value: str) -> bool:
    candidate = Path(value).expanduser()
    return candidate.exists()


_COMMIT = re.compile(r"[0-9a-f]{7,64}")
#: Transports a marketplace-declared source may use; ``file`` and ``ext`` never.
_DECLARED_URL = re.compile(r"(?:https|ssh|git)://[^\s]+|[\w.-]+@[\w.-]+:[^\s]+")
_PROTOCOLS = (
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "protocol.ssh.allow=always",
    "-c",
    "protocol.git.allow=always",
)


def _check_revision(source: str, ref: str, commit: str) -> None:
    if commit and not _COMMIT.fullmatch(commit):
        raise SourceError(f"{source}: commit {commit!r} is not a hexadecimal commit id")
    if ref.startswith("-"):
        raise SourceError(f"{source}: ref {ref!r} is not a branch or tag name")
    if ref:
        try:
            _git(["check-ref-format", "--allow-onelevel", ref], timeout=10)
        except SourceError as exc:
            raise SourceError(f"{source}: ref {ref!r} is not a branch or tag name") from exc


def _check_declared(url: str) -> None:
    if url.startswith("-") or not _DECLARED_URL.fullmatch(url):
        raise SourceError(
            f"{url!r}: a marketplace may only point at an https, ssh or git URL, "
            "never a folder on this machine"
        )


def _git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    verb = " ".join(a for a in args if not a.startswith(("-", "protocol.")))[:40]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed git argv
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"git {verb} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:] or [""]
        raise SourceError(f"git {verb} failed: {detail[0]}")
    return completed.stdout.strip()


def fetch(
    source: str, *, cache: Path, ref: str = "", commit: str = "", declared: bool = False
) -> FetchedSource:
    """Fetch ``source`` and return a local path holding exactly that revision.

    ``declared`` marks a source a marketplace file names (a git plugin or a
    federated marketplace) rather than one the person typed: it must be a network URL.
    """

    _check_revision(source, ref, commit)
    if declared:
        _check_declared(normalize_git_url(source))
    elif is_local(source) and not commit:
        path = Path(source).expanduser().resolve()
        head = ""
        if (path / ".git").exists():
            try:
                head = _git(["rev-parse", "HEAD"], cwd=path, timeout=30)
            except SourceError as exc:
                # A local folder installs without a commit; say why it has none.
                logger.warning(
                    "plugin source commit unknown reason=local_git_head_unreadable path=%s error=%s",
                    path,
                    exc,
                )
                return FetchedSource(
                    path=path,
                    url=str(path),
                    commit="",
                    local=True,
                    commit_reason="local_git_head_unreadable",
                )
        return FetchedSource(
            path=path,
            url=str(path),
            commit=head,
            local=True,
            commit_reason="" if head else "local_folder_not_git",
        )
    local = not declared and is_local(source)
    url = str(Path(source).expanduser().resolve()) if local else normalize_git_url(source)
    if url.startswith("-"):
        raise SourceError(f"{url!r} is not a git URL")
    key = hashlib.sha256(f"{url}\0{commit or ref}".encode()).hexdigest()[:20]
    target = cache / key
    if commit and (target / ".git").exists():
        return FetchedSource(path=target, url=url, commit=commit, local=False)
    staging = cache / f".{key}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    _git(["init", "-q"], cwd=staging, timeout=60)
    _git(["remote", "add", "--", "origin", url], cwd=staging, timeout=30)
    wanted = commit or ref or "HEAD"
    protocols = [] if local else list(_PROTOCOLS)
    _git([*protocols, "fetch", "-q", "--depth", "1", "--", "origin", wanted], cwd=staging)
    _git(["checkout", "-q", "--detach", "FETCH_HEAD", "--"], cwd=staging, timeout=120)
    head = _git(["rev-parse", "HEAD"], cwd=staging, timeout=30)
    if commit and not head.startswith(commit[:12]):
        raise SourceError(f"{url}: fetched {head} but the pin is {commit}")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return FetchedSource(path=target, url=url, commit=head, local=False)
