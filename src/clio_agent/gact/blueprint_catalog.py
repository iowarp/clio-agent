"""Installed snapshots and explicitly selectable marketplace entries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from clio_agent.gact.agent_blueprint_sources import (
    load_agent_blueprint_sources,
    source_registry_id,
)
from clio_agent.gact.agent_blueprints import (
    AgentBlueprintDefinition,
    discover_agent_blueprints,
    install_agent_blueprint,
    parse_agent_blueprint_root,
)
from clio_agent.gact.blueprint_identity import AmbiguousBlueprintError, select_blueprint


def blueprint_catalog(*, cwd: Path | None, workspace_id: str = "") -> list[dict[str, Any]]:
    """List available choices without installing or refreshing a marketplace.

    Uninstalled entries remain selectable, including explicit uninstalls. Only
    choosing one materializes it again; browsing this catalog never clears a
    tombstone. Workspace registrations remain confined to their owning workspace.
    """
    rows = {
        row["identity"]: {**row, "materialized": True}
        for blueprint in discover_agent_blueprints(cwd=cwd)
        for row in [blueprint.to_wire()]
    }
    for source in load_agent_blueprint_sources():
        scope = str(source.get("install_scope") or "global")
        if scope == "workspace" and source.get("workspace_id") != workspace_id:
            continue
        if scope not in {"global", "workspace"}:
            continue
        source_id = str(source["id"])
        for candidate in source.get("available_blueprints") or []:
            identifier = f"{scope}::{source_id}::{candidate['id']}"
            rows.setdefault(
                identifier,
                {
                    **candidate,
                    # A remote discovery checkout was temporary. Never offer
                    # its deleted path as an installed runtime or file browser.
                    "definition_path": "",
                    "root": "",
                    "identity": identifier,
                    "source_id": source_id,
                    "registry_id": source["id"],
                    "scope": scope,
                    "materialized": False,
                    "metadata": {
                        "install": {
                            "source": source["source"],
                            "ref": source.get("ref") or "",
                            "pinned_commit": source.get("pinned_commit") or "",
                        },
                        "marketplace_name": source.get("name") or source["source"],
                    },
                },
            )
    return list(rows.values())


def resolve_blueprint_choice(
    identifier: str, *, cwd: Path | None, workspace_id: str = ""
) -> dict[str, Any]:
    """Resolve one installed or available identity without materializing it."""
    choices = [
        row
        for row in blueprint_catalog(cwd=cwd, workspace_id=workspace_id)
        if identifier in {row["id"], row["identity"]}
    ]
    if len(choices) > 1:
        raise AmbiguousBlueprintError(
            f"Blueprint {identifier!r} is ambiguous; select one of: "
            + ", ".join(row["identity"] for row in choices)
        )
    if not choices:
        raise FileNotFoundError(f"agent blueprint not found: {identifier}")
    return choices[0]


def materialize_blueprint(
    identifier: str, *, cwd: Path | None, workspace_id: str = "", app: Any = None
) -> AgentBlueprintDefinition:
    """Resolve an unambiguous choice and install it if necessary on this CLIO.

    Call inside the runtime revision boundary. Existing snapshots are reused;
    selecting one never implicitly reloads changed source files or changes a pin.
    """
    row = resolve_blueprint_choice(identifier, cwd=cwd, workspace_id=workspace_id)
    if row["materialized"]:
        return parse_agent_blueprint_root(Path(row["root"]), scope=row["scope"])
    install = row["metadata"]["install"]
    if row["scope"] == "workspace" and cwd is None:
        raise ValueError("workspace-scoped installation requires a workspace root")
    result = install_agent_blueprint(
        source=install["source"],
        source_id=row["source_id"],
        ref=install["ref"],
        pinned_commit=install["pinned_commit"],
        blueprint_id=row["id"],
        scope=cast(Literal["global", "workspace"], row["scope"]),
        cwd=cwd or Path.cwd(),
        app=app,
    )
    installed = result["installed"][0]
    return parse_agent_blueprint_root(Path(installed["root"]), scope=installed["scope"])


def materialize_blueprint_path(
    source: Path, *, cwd: Path | None, app: Any
) -> AgentBlueprintDefinition:
    """Snapshot a validated local authoring folder before using it in a session."""
    if cwd is None:
        raise ValueError("path activation requires a workspace root")
    source = source.expanduser().resolve()
    if source.name == "AGENT.md":
        source = source.parent
    parsed = parse_agent_blueprint_root(source, scope="workspace")
    identifier = f"workspace::{source_registry_id(str(source))}::{parsed.id}"
    installed = select_blueprint(discover_agent_blueprints(cwd=cwd), identifier)
    if installed is not None:
        return installed
    result = install_agent_blueprint(
        source=str(source), scope="workspace", cwd=cwd, blueprint_id=parsed.id, app=app
    )
    return parse_agent_blueprint_root(Path(result["installed"][0]["root"]), scope="workspace")
