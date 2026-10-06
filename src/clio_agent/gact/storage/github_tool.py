"""Managed gh reads scoped to an approved GitHub source and CLIO account."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
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
    elif folder or not re.fullmatch(r"(?:branches|tags|commits)(?:/[A-Za-z0-9._-]+)?|", tail):
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


def github_cli(source_id: str, arguments: list[str]) -> dict[str, Any]:
    """Read an approved GitHub repository with CLIO's managed gh and signed-in account.

    Use ['repo', 'view'] or ['api', 'repos/OWNER/REPO/contents/PATH'],
    branches, tags or commits. Repository/folder scope is enforced; no CLI login,
    tokens, extensions, local files or writes. Sign in with the status tool's
    ordinary A2UI login action when needed. Connect a requested source with
    connected_data_connect first. Edit through connected_data_write, which
    retains the source's review and publication rules. Do not use host-shell gh
    as a substitute for CLIO account access.
    """
    app = context.active_app()
    sid = context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None:
        raise ValueError("GitHub CLI requires an active workspace session")
    service = app.state.connected_storage
    record = service.get(session.workspace_id, source_id, connected=True)
    return source_cli(service, record, arguments)


def build_github_cli_tool() -> Any:
    """Expose managed, source-bound GitHub reads as an observed native tool."""
    return native_tool(
        github_cli,
        name="github_cli",
        desc=github_cli.__doc__,
        args={
            "source_id": {"type": "string"},
            "arguments": {"type": "array", "items": {"type": "string"}},
        },
        title="GitHub CLI",
        presentation="fields:source_id,command,access,result",
        domain="resources",
        read_only=True,
    )
