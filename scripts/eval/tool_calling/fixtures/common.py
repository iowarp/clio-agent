"""Shared fixture helpers: seeded randomness, hashing, and deterministic git."""

from __future__ import annotations

import hashlib
import io
import os
import random
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path

#: Identity and clock for every fixture commit, so the same content gives the same SHAs.
FIXTURE_AUTHOR = ("Fixture Author", "fixture@example.invalid")
FIXTURE_EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z


def derive_rng(seed: int, label: str) -> random.Random:
    """An independent, reproducible stream for one fixture."""
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def sha256_file(path: Path) -> str:
    """The sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(root: Path, *, skip: frozenset[str] = frozenset({".git"})) -> dict[str, str]:
    """``{posix relative path: sha256}`` for every file under ``root``, sorted.

    Directories named in ``skip`` (git internals by default) are not hashed;
    a repository fixture records its HEAD commit in its answer key instead.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in skip for part in rel.parts) or not path.is_file():
            continue
        out[rel.as_posix()] = sha256_file(path)
    return out


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with ``\\n`` line endings, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def reset_dir(path: Path) -> Path:
    """Empty ``path`` (created if missing). Refuses to touch a git checkout root."""
    if (path / ".git").exists() and (path / "pyproject.toml").exists():
        raise ValueError(f"refusing to reset what looks like a checkout: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _git_env(when: int) -> dict[str, str]:
    stamp = f"{when} +0000"
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": FIXTURE_AUTHOR[0],
        "GIT_AUTHOR_EMAIL": FIXTURE_AUTHOR[1],
        "GIT_COMMITTER_NAME": FIXTURE_AUTHOR[0],
        "GIT_COMMITTER_EMAIL": FIXTURE_AUTHOR[1],
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        # User and system configuration (signing, hooks, templates) must not
        # change the commits, so neither is read.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def git(repo: Path, *args: str, when: int = FIXTURE_EPOCH) -> str:
    """Run git in ``repo`` with the fixture identity; return stdout."""
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=_git_env(when),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def write_state(repo: Path, files: Mapping[str, str | None]) -> None:
    """Write (or, for ``None``, delete) files in a working tree."""
    for rel, text in files.items():
        target = repo / rel
        if text is None:
            target.unlink(missing_ok=True)
        else:
            write_text(target, text)


def build_history(repo: Path, commits: list[tuple[str, Mapping[str, str | None]]]) -> list[str]:
    """Create a fresh repository with one commit per ``(message, files)``; return the SHAs.

    Each commit is one hour after the previous one, so the history is identical
    for identical content.
    """
    reset_dir(repo)
    git(repo, "init", "-q", "-b", "main")
    shas: list[str] = []
    for index, (message, files) in enumerate(commits):
        write_state(repo, files)
        when = FIXTURE_EPOCH + 3600 * index
        git(repo, "add", "-A", when=when)
        git(repo, "commit", "-q", "-m", message, when=when)
        shas.append(git(repo, "rev-parse", "HEAD").strip())
    return shas


def archive_tree(source_repo: Path, commit: str, path: str, dest: Path) -> str:
    """Extract ``git archive <commit> <path>`` into ``dest``; return the full commit SHA."""
    full = git(source_repo, "rev-parse", f"{commit}^{{commit}}").strip()
    blob = subprocess.run(
        ["git", "archive", "--format=tar", full, path],
        cwd=source_repo,
        capture_output=True,
        check=False,
    )
    if blob.returncode != 0:
        raise RuntimeError(f"git archive {full} {path}: {blob.stderr.decode().strip()}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob.stdout)) as archive:
        archive.extractall(dest, filter="data")
    return full
