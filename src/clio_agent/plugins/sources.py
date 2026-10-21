"""Fetch plugin sources: local folders and git repositories pinned by commit.

A source is a local path, a git URL (``https://...git``, ``github:owner/repo``)
with an optional ref, or a git URL pinned to a commit. Pinned fetches are
content-addressed by ``(url, commit)`` so two marketplace entries that point
into the same commit (federated ``git-subdir`` entries) share one checkout.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SourceError(RuntimeError):
    """A source could not be fetched. The message names the source and the cause."""


@dataclass(frozen=True)
class FetchedSource:
    path: Path
    url: str
    commit: str
    local: bool


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


def _git(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
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
        raise SourceError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:] or [""]
        raise SourceError(f"git {' '.join(args[:2])} failed: {detail[0]}")
    return completed.stdout.strip()


def fetch(source: str, *, cache: Path, ref: str = "", commit: str = "") -> FetchedSource:
    """Fetch ``source`` and return a local path holding exactly that revision."""

    if is_local(source) and not commit:
        path = Path(source).expanduser().resolve()
        head = ""
        if (path / ".git").exists():
            try:
                head = _git(["rev-parse", "HEAD"], cwd=path, timeout=30)
            except SourceError:
                head = ""
        return FetchedSource(path=path, url=str(path), commit=head, local=True)
    url = normalize_git_url(source) if not is_local(source) else str(Path(source).resolve())
    key = hashlib.sha256(f"{url}\0{commit or ref}".encode()).hexdigest()[:20]
    target = cache / key
    if commit and (target / ".git").exists():
        return FetchedSource(path=target, url=url, commit=commit, local=False)
    staging = cache / f".{key}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    _git(["init", "-q"], cwd=staging, timeout=60)
    _git(["remote", "add", "origin", url], cwd=staging, timeout=30)
    wanted = commit or ref or "HEAD"
    _git(["fetch", "-q", "--depth", "1", "origin", wanted], cwd=staging)
    _git(["checkout", "-q", "FETCH_HEAD"], cwd=staging, timeout=120)
    head = _git(["rev-parse", "HEAD"], cwd=staging, timeout=30)
    if commit and not head.startswith(commit[:12]):
        raise SourceError(f"{url}: fetched {head} but the pin is {commit}")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return FetchedSource(path=target, url=url, commit=head, local=False)
