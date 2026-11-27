"""Publish the private marketplace to a GitHub repository the person names.

The files are exactly the backup ZIP's (``local_marketplace_files``). One commit
brings the repository's marketplace folders to match this host: changed files are
written, files the host no longer has are removed, and anything else in the
repository (a README, a licence, CI) is left alone. Nothing is committed when the
repository already matches. Publishing uses the person's own CLIO GitHub sign-in;
the repository must already exist and be reachable by CLIO's GitHub app.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from clio_agent.gact.storage.github_filesystem import commit_tree
from clio_agent.plugins.local_export import LocalExportError, local_marketplace_files
from clio_agent.plugins.marketplace import NATIVE_INDEX

if TYPE_CHECKING:
    from clio_agent.plugins.service import PluginService

#: The repository folders a publication owns; everything else in it is the person's.
MANAGED = ("plugins/", "agents/", "user-agents/", "ai.iowarp.clio/")
STATE_FILE = "local-publish.json"
_API = "https://api.github.com/repos/"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
_REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")


class PublishError(ValueError):
    """A refused publication: an HTTP status, a typed code and what the person can do."""

    def __init__(self, status: int, code: str, message: str, **details: Any) -> None:
        self.status, self.code, self.details = status, code, details
        super().__init__(message)


def parse_repository(text: str) -> str:
    """``owner/name`` from ``owner/name``, a github.com address or a clone URL."""
    value = text.strip().removesuffix("/").removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github:"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    if not _REPOSITORY.fullmatch(value) or value.split("/")[1] in {".", ".."}:
        raise PublishError(
            422,
            "repository_invalid",
            "Name the repository as owner/name or paste its github.com address.",
        )
    return value


def blob_sha(data: bytes) -> str:
    """Git's object id for a file's bytes, to compare with the repository's tree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def files_digest(files: dict[str, tuple[bytes, int]]) -> str:
    """One digest of every path and content, to tell whether anything changed since."""
    digest = hashlib.sha256()
    for path, (data, _mode) in sorted(files.items()):
        digest.update(path.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def _state_path(service: PluginService) -> Path:
    return service.store.root / STATE_FILE


def last_publication(service: PluginService) -> dict[str, Any] | None:
    """The last publication recorded on this host, or None."""
    try:
        value = json.loads(_state_path(service).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PublishError(
            409, "publish_record_unreadable", f"The last publication record is unreadable: {exc}"
        ) from exc
    return value if isinstance(value, dict) else None


def _previous_commit(service: PluginService, repository: str) -> str:
    last = last_publication(service) or {}
    return str(last.get("commit", "")) if last.get("repository") == repository else ""


def status(service: PluginService) -> dict[str, Any]:
    """The last publication and whether the collection changed since."""
    last = last_publication(service)
    try:
        digest = files_digest(local_marketplace_files(service))
        empty = False
    except LocalExportError as exc:
        if exc.status != 404:
            raise PublishError(exc.status, "local_export_refused", str(exc)) from exc
        digest, empty = "", True
    return {
        "last": last,
        "empty": empty,
        "changed_since": bool(last) and last.get("digest") != digest,
    }


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    response = client.request(method, url, **kwargs)
    if response.status_code == 401:
        raise PublishError(
            401, "github_sign_in_expired", "Your GitHub sign-in expired. Sign in again."
        )
    return response


def _repository(client: httpx.Client, repository: str, account_url: str) -> dict[str, Any]:
    response = _request(client, "GET", _API + repository)
    if response.status_code == 403:
        raise PublishError(
            403,
            "github_refused",
            "GitHub refused the request (a rate limit or an organization policy). "
            "Try again later, or check the organization's access rules.",
        )
    if response.status_code == 404:
        raise PublishError(
            404,
            "repository_not_reachable",
            f"CLIO cannot reach {repository} with your GitHub sign-in. Create the repository "
            "first, then give CLIO's GitHub app access to it.",
            account_url=account_url,
            repository=repository,
        )
    response.raise_for_status()
    return response.json()


def _initialize(client: httpx.Client, repository: str, files: dict[str, tuple[bytes, int]]) -> None:
    """Give an empty repository its first commit (Git data writes need one)."""
    index = NATIVE_INDEX.as_posix()
    response = _request(
        client,
        "PUT",
        _API + repository + "/contents/" + quote(index),
        json={
            "message": "Start the CLIO plugin backup",
            "content": base64.b64encode(files[index][0]).decode("ascii"),
        },
    )
    if response.status_code >= 400:
        raise PublishError(
            409,
            "repository_not_writable",
            "GitHub refused the first commit to this empty repository. Check that your "
            "account and CLIO's GitHub app can write to it.",
        )


def _remote_tree(client: httpx.Client, repository: str, branch: str) -> dict[str, str] | None:
    """Every file path and blob id on the branch, or None for an empty repository."""
    ref = _request(client, "GET", f"{_API}{repository}/git/ref/heads/{quote(branch, safe='')}")
    if ref.status_code in {404, 409}:
        return None
    ref.raise_for_status()
    head = ref.json()["object"]["sha"]
    tree = _request(client, "GET", f"{_API}{repository}/git/commits/{head}").json()["tree"]["sha"]
    listing = _request(client, "GET", f"{_API}{repository}/git/trees/{tree}?recursive=1").json()
    if listing.get("truncated"):
        raise PublishError(
            409, "repository_too_large", "This repository is too large to publish into safely."
        )
    return {row["path"]: row["sha"] for row in listing["tree"] if row.get("type") == "blob"}


def publish(
    service: PluginService,
    token: str,
    repository_text: str,
    *,
    allow_public: bool = False,
    account_url: str = "",
) -> dict[str, Any]:
    """Bring the repository's marketplace folders to match this host in one commit."""
    repository = parse_repository(repository_text)
    try:
        files = local_marketplace_files(service)
    except LocalExportError as exc:
        code = "local_collection_empty" if exc.status == 404 else "local_export_refused"
        raise PublishError(exc.status, code, str(exc)) from exc
    headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=False) as client:
        info = _repository(client, repository, account_url)
        if not info.get("permissions", {}).get("push"):
            raise PublishError(
                403,
                "repository_not_writable",
                f"Your GitHub account cannot write to {repository}.",
                repository=repository,
            )
        if not info.get("private") and not allow_public:
            raise PublishError(
                409,
                "repository_public",
                f"{repository} is public: anyone could read these plugins and agents. "
                "Publish anyway, or choose a private repository.",
                repository=repository,
            )
        branch = str(info.get("default_branch") or "main")
        remote = _remote_tree(client, repository, branch)
        if remote is None:
            _initialize(client, repository, files)
            remote = _remote_tree(client, repository, branch) or {}
    changes: list[tuple[str, Path | bytes | None, str | None]] = [
        (path, data, remote.get(path))
        for path, (data, _mode) in sorted(files.items())
        if remote.get(path) != blob_sha(data)
    ]
    removed = sorted(path for path in remote if path.startswith(MANAGED) and path not in files)
    changes.extend((path, None, remote[path]) for path in removed)
    url = f"https://github.com/{repository}"
    commit = ""
    if changes:
        written = len(changes) - len(removed)
        owner, name = repository.split("/")
        try:
            commit, _blobs = commit_tree(
                owner,
                name,
                branch,
                token,
                changes,
                f"Back up CLIO plugins and agents ({written} written, {len(removed)} removed)",
            )
        except PermissionError as exc:
            raise PublishError(403, "repository_not_writable", str(exc)) from exc
        except ValueError as exc:
            raise PublishError(409, "repository_changed", str(exc)) from exc
    record = {
        "repository": repository,
        "url": url,
        "branch": branch,
        "commit": commit or _previous_commit(service, repository),
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "digest": files_digest(files),
        "private": bool(info.get("private")),
    }
    path = _state_path(service)
    staging = path.with_suffix(".writing")
    staging.write_text(json.dumps(record, indent=2), encoding="utf-8")
    staging.replace(path)
    return {
        **record,
        "up_to_date": not changes,
        "written": sorted(p for p, content, _ in changes if content is not None),
        "removed": removed,
        "commit_url": f"{url}/commit/{commit}" if commit else "",
    }
