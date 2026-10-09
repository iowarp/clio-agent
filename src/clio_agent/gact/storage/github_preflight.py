"""Internal authenticated preflight for an explicitly connected GitHub source."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from clio_agent.gact.storage.linked import github_location
from clio_agent.gact.storage.models import SourceRecord
from clio_agent.gact.storage.service import StorageService
from clio_agent.runtime.github_cli import run_github_cli


def source_arguments(record: SourceRecord, arguments: list[str]) -> list[str]:
    """Allow bounded gh reads of the selected repository and folder only.

    Never permit extension/alias execution, auth-token output, a different host,
    local-file flags, writes, or an agent-supplied repository override.
    """
    org, repo, ref, folder = github_location(record.source.root, record.configuration.github_ref)
    base = f"repos/{org}/{repo}"
    if arguments == ["repo", "view"] and not folder:
        return [
            "repo",
            "view",
            f"{org}/{repo}",
            "--json",
            "name,url,description,isPrivate,defaultBranchRef",
        ]
    if len(arguments) != 2 or arguments[0] != "api":
        raise ValueError("Use ['repo', 'view'] or ['api', 'repos/OWNER/REPO/...'] for a read")
    endpoint = arguments[1]
    parsed = urlsplit(endpoint)
    path = unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(part in {".", "..", ""} for part in path.split("/"))
        or "\\" in path
        or any(ord(character) < 32 for character in endpoint)
        or "%" in path
    ):
        raise ValueError("Use a plain repository API path without escapes or query flags")
    if path != base and not path.startswith(base + "/"):
        raise PermissionError("GitHub CLI is restricted to the approved repository")
    tail = path.removeprefix(base).removeprefix("/")
    if tail.startswith("contents/") or tail == "contents":
        relative = tail.removeprefix("contents").removeprefix("/")
        if folder and relative != folder and not relative.startswith(folder + "/"):
            raise PermissionError("GitHub CLI is restricted to the approved folder")
    elif folder or not re.fullmatch(
        r"(?:branches|tags|commits)(?:/[A-Za-z0-9._-]+)?"
        r"|releases(?:/latest|/[0-9]+|/tags/[A-Za-z0-9._-]+)?|",
        tail,
    ):
        raise PermissionError("This GitHub API resource is outside the source read scope")
    if tail.startswith("contents") and ref:
        from urllib.parse import quote

        endpoint += "?ref=" + quote(ref, safe="")
    return ["api", endpoint, "--method", "GET"]


def source_cli(
    service: StorageService, record: SourceRecord, arguments: list[str]
) -> dict[str, Any]:
    """Resolve a fresh private grant and invoke the pinned CLI for this source."""
    if not record.connected or record.source.provider != "github":
        raise PermissionError("Use an approved, connected GitHub source")
    if not service.auth.connected(record):
        raise PermissionError("Sign in to GitHub in CLIO before using this source")
    command = source_arguments(record, arguments)
    completed = run_github_cli(
        command,
        token=service.auth.token(record),
        config_root=service.auth.private_root / "github-cli",
    )
    if completed.returncode:
        raise PermissionError(
            "GitHub rejected the source read. Check CLIO sign-in, repository access and app permissions. "
            + completed.stderr[:2000]
        )
    if len(completed.stdout.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("GitHub output exceeds 4 MiB; open a specific source file instead")
    return {
        "source_id": record.source.id,
        "command": ["gh", *command],
        "access": record.linked_access,
        "result": json.loads(completed.stdout),
    }
