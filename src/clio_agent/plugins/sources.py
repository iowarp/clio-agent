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

A machine with no usable git (a Mac without the Command Line Tools whose install came
from PyPI, a minimal Linux or Windows) still fetches a GitHub source: GitHub's archive
of the same revision is unpacked instead of a clone (:func:`_fetch_archive`), so the
default marketplace installs during the install wherever git is missing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import subprocess
import tarfile
import time
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from clio_agent.platform_paths import remove_tree, rename_extended
from clio_agent.runtime.git_executable import GitUnavailableError, run_git


class SourceError(RuntimeError):
    """A source could not be fetched. The message names the source and the cause."""


class GitMissingError(SourceError):
    """This machine has no usable git (the message says what to install)."""


logger = logging.getLogger(__name__)

#: Seconds a fetch's staging folder is left alone, in case that fetch is still running.
STAGING_GRACE_SECONDS = 600.0


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


def cache_key(url: str, revision: str) -> str:
    """The fetch cache folder name of ``url`` at ``revision`` (a commit or a ref)."""
    return hashlib.sha256(f"{url}\0{revision}".encode()).hexdigest()[:20]


def fetch_cache_names(entry: dict[str, Any]) -> set[str]:
    """The fetch cache folders an installed marketplace ``entry``'s install filled (``fetch_cache``).

    An entry installed before that was recorded names only its own checkout, derived
    from its URL and revision the way :func:`fetch` keys it.
    """
    if "fetch_cache" in entry:
        return {str(name) for name in entry.get("fetch_cache") or []}
    url = str(entry.get("url") or "")
    if not url:
        return set()
    revision = entry.get("commit", "") if entry.get("pinned") else entry.get("ref", "")
    return {cache_key(url, str(revision or ""))}


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
#: A GitHub https repository URL, the only kind fetched without git (``owner``, ``repo``).
_GITHUB_HTTPS = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
#: What ``git check-ref-format --allow-onelevel`` accepts, for a machine without git.
_REF = re.compile(r"(?!.*(?:\.\.|//|@\{|\.lock/|/\.))(?![/.])[A-Za-z0-9._/+-]+(?<![/.])(?<!\.lock)")
#: The largest repository archive a git-less fetch reads (a marketplace is about 1 MB).
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024


def _check_revision(source: str, ref: str, commit: str) -> None:
    if commit and not _COMMIT.fullmatch(commit):
        raise SourceError(f"{source}: commit {commit!r} is not a hexadecimal commit id")
    if ref.startswith("-"):
        raise SourceError(f"{source}: ref {ref!r} is not a branch or tag name")
    if ref:
        try:
            _git(["check-ref-format", "--allow-onelevel", ref], timeout=10)
        except GitMissingError:
            if not _REF.fullmatch(ref):
                raise SourceError(f"{source}: ref {ref!r} is not a branch or tag name") from None
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
    if os.name == "nt":
        # A marketplace's deepest files can pass MAX_PATH below a long store root.
        args = ["-c", "core.longpaths=true", *args]
    try:
        completed = run_git(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except GitUnavailableError as exc:
        raise GitMissingError(f"git {verb} failed: {exc}") from exc
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
    key = cache_key(url, commit or ref)
    target = cache / key
    if commit and (target / ".git").exists():
        return FetchedSource(path=target, url=url, commit=commit, local=False)
    _sweep_staging(cache, key, older_than=STAGING_GRACE_SECONDS)
    # Each fetch stages in its own folder, so a leftover nothing could delete (an
    # earlier crash, a file another process held) never blocks the next fetch.
    staging = cache / f".{key}.partial-{uuid.uuid4().hex[:12]}"
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        raise SourceError(f"cannot create the fetch folder {staging}: {exc}") from exc
    try:
        wanted = commit or ref or "HEAD"
        try:
            _git(["init", "-q"], cwd=staging, timeout=60)
        except GitMissingError:
            archive = None if local else github_archive_url(url, wanted)
            if archive is None:
                raise
            head = _fetch_archive(archive, staging, url=url)
        else:
            _git(["remote", "add", "--", "origin", url], cwd=staging, timeout=30)
            protocols = [] if local else list(_PROTOCOLS)
            _git([*protocols, "fetch", "-q", "--depth", "1", "--", "origin", wanted], cwd=staging)
            _git(["checkout", "-q", "--detach", "FETCH_HEAD", "--"], cwd=staging, timeout=120)
            head = _git(["rev-parse", "HEAD"], cwd=staging, timeout=30)
        if commit and not head.startswith(commit[:12]):
            raise SourceError(f"{url}: fetched {head} but the pin is {commit}")
        try:
            remove_tree(target)
        except OSError as exc:
            raise SourceError(f"{url}: cannot replace the previous checkout: {exc}") from exc
        try:
            rename_extended(staging, target)
        except OSError as exc:
            raise SourceError(f"{url}: cannot move the checkout into {target}: {exc}") from exc
    except BaseException:
        _discard(staging)
        raise
    return FetchedSource(path=target, url=url, commit=head, local=False)


def github_archive_url(url: str, revision: str) -> str | None:
    """GitHub's tar.gz archive of ``url`` at ``revision``, or ``None`` for another host."""
    match = _GITHUB_HTTPS.fullmatch(url)
    if match is None:
        return None
    owner, repo = match.groups()
    return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{quote(revision, safe='/')}"


class _Capped:
    """A response read through, refusing to read past ``limit`` bytes."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.read_bytes += len(data)
        if self.read_bytes > self._limit:
            raise SourceError(f"the archive is larger than {self._limit} bytes")
        return data


def _open_archive(archive: str) -> BinaryIO:
    import truststore  # noqa: PLC0415 - a dependency; only a git-less fetch needs it

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    request = urllib.request.Request(archive, headers={"User-Agent": "clio-agent"})
    return urllib.request.urlopen(request, timeout=60, context=context)  # noqa: S310 - https


def _fetch_archive(archive: str, staging: Path, *, url: str) -> str:
    """Unpack GitHub's ``archive`` into ``staging``; returns the commit it holds.

    The same tree a shallow clone checks out, without ``.git``: GitHub names the
    commit in the archive's global header, and ``fetch`` checks it against the pin.
    Members are unpacked with the ``data`` filter (no absolute paths, nothing outside
    ``staging``, no devices) below the archive's one top folder.
    """
    started = time.monotonic()
    try:
        with _open_archive(archive) as response:
            capped = _Capped(response, ARCHIVE_MAX_BYTES)
            with tarfile.open(fileobj=capped, mode="r|gz") as bundle:  # type: ignore[call-overload]

                def members() -> Iterator[tarfile.TarInfo]:
                    for member in bundle:
                        _, _, inner = member.name.partition("/")
                        if not inner:
                            continue
                        member.name = inner
                        if member.islnk():
                            member.linkname = member.linkname.partition("/")[2]
                        yield member

                bundle.extractall(staging, members=members(), filter="data")
                head = str(bundle.pax_headers.get("comment", "")).strip()
    except (SourceError, OSError, tarfile.TarError, ValueError) as exc:
        raise SourceError(f"{url}: no git here, and GitHub's archive failed: {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
        raise SourceError(f"{url}: GitHub's archive names no commit")
    logger.info(
        "plugin source fetched without git reason=git_unavailable url=%s commit=%s "
        "bytes=%d seconds=%.1f",
        url,
        head,
        capped.read_bytes,
        time.monotonic() - started,
    )
    return head


def _discard(staging: Path) -> None:
    try:
        remove_tree(staging)
    except OSError as exc:
        # Uniquely named, so it blocks nothing; the next fetch or an uninstall retries.
        logger.warning("plugin source staging left behind path=%s error=%s", staging, exc)


def _sweep_staging(cache: Path, key: str, *, older_than: float) -> None:
    """Delete ``key``'s staging folders left by a fetch that stopped (logged, never fatal).

    The un-suffixed ``.<key>.partial`` of earlier releases is never in use; a suffixed
    one younger than ``older_than`` seconds may be a fetch still running.
    """
    if not cache.is_dir():
        return
    now = time.time()
    for folder in cache.glob(f".{key}.partial*"):
        if folder.name != f".{key}.partial":
            try:
                if now - folder.stat().st_mtime < older_than:
                    continue
            except OSError:
                continue
        try:
            remove_tree(folder)
        except OSError as exc:
            logger.warning("stale plugin source staging kept path=%s error=%s", folder, exc)


def remove_cached(cache: Path, name: str) -> None:
    """Delete the fetched checkout ``cache/name`` and every staging folder of it.

    Raises:
        TreeRemovalError: a folder could not be deleted (it names the folder).
    """
    if not name or name != Path(name).name or name.startswith("."):
        return
    remove_tree(cache / name)
    if cache.is_dir():
        for folder in cache.glob(f".{name}.partial*"):
            remove_tree(folder)
