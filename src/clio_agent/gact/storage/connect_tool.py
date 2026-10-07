"""Agent source attachment through the normal session permission gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.permission_gate import _invoke_permission_gate
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.setup_tool import connected_data_status, setup_presentation


def connected_data_connect(
    provider: str,
    root: str,
    label: str,
    mode: str = "read_only",
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a requested source to this workspace after permission approval.

    Reuse an approved source with the same provider, location and access mode.
    A new connection uses the normal approval gate, including bypass mode.
    Default to read_only; working_copy stages edits for review, write_enabled
    changes originals. Sign-in remains private and user-operated; if missing,
    return the login action for an A2UI Button in the answer. No credentials
    may be passed in configuration. Linking exposes references, not a shell mount.
    """
    app = context.active_app()
    sid = context.active_session_id()
    session = app.state.sessions.get(sid) if app is not None and sid else None
    if app is None or session is None:
        raise ValueError("Source connection requires an active CLIO workspace session")
    workspace = app.state.workspaces.get(session.workspace_id)
    if workspace is None:
        raise ValueError("The session workspace is unavailable on this CLIO")
    service = app.state.connected_storage
    body = CreateSource.model_validate(
        {
            "provider": provider,
            "root": root,
            "label": label,
            "mode": mode,
            "configuration": configuration or {},
        }
    )
    prepared = service._prepare_source(session.workspace_id, body, Path(workspace.root_path))
    existing = next(
        (
            record
            for record in service.sources(session.workspace_id)
            if record.connected
            and record.principal == service.principal
            and record.source.owner.clio_id == service.store.clio_id
            and record.source.provider == prepared.source.provider
            and record.source.root == prepared.source.root
            and record.source.mode == prepared.source.mode
            and record.configuration == prepared.configuration
        ),
        None,
    )
    if existing is None:
        gate = (
            getattr(app.state, "pending_permission_gate", None) or app.state.make_permission_gate()
        )
        args = body.model_dump(mode="json")
        if (
            _invoke_permission_gate(gate, "connected_data_connect", args, {"kind": "native"})
            != "allow"
        ):
            raise PermissionError("Source connection was not approved")
        service.store.put("source", prepared.source.id, prepared)
        existing = prepared
    if provider in {"github", "google_drive", "globus"} and not service.auth.connected(existing):
        result = connected_data_status()
        result["connection"] = {"source_id": existing.source.id, "state": "sign_in_required"}
        return result
    if not existing.linked_manifest_id:
        if provider == "github":
            from clio_agent.gact.storage.github_tool import source_cli
            from clio_agent.gact.storage.linked import github_location

            org, repo, _, folder = github_location(
                existing.source.root, existing.configuration.github_ref
            )
            source_cli(
                service,
                existing,
                ["api", f"repos/{org}/{repo}/contents" + (f"/{folder}" if folder else "")],
            )
        link_folder(service, existing)
    result = connected_data_status()
    result["connection"] = {"source_id": existing.source.id, "state": "linked"}
    return result


def build_connected_data_connect_tool() -> Any:
    """Declare source attachment as an effectful, observed native tool."""
    return native_tool(
        connected_data_connect,
        name="connected_data_connect",
        desc=connected_data_connect.__doc__,
        args={
            "provider": {"type": "string", "description": "Provider reported by source status."},
            "root": {"type": "string", "description": "Requested folder or repository location."},
            "label": {"type": "string", "description": "User-facing name for this source."},
            "mode": {"type": "string", "description": "read_only, working_copy or write_enabled."},
            "configuration": {"type": "object", "description": "Nonsecret provider settings."},
        },
        presentation=setup_presentation,
        title="Connect source",
        domain="resources",
        read_only=False,
    )
