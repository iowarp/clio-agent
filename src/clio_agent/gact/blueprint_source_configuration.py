"""Editable marketplace configuration, separate from the installed runtime revision."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clio_agent.gact import agent_blueprint_sources as registry
from clio_agent.gact.blueprint_mutations import BLUEPRINT_MUTATION_LOCK


class SourceConfigurationConflict(ValueError):
    """The registration changed while an operation was being prepared."""


def registered_source(source_id: str) -> dict[str, Any]:
    """Return the current owning registration, or an empty row when forgotten."""
    return next(
        (row for row in registry.load_agent_blueprint_sources() if row["id"] == source_id), {}
    )


def registration_id(source: str, ref: str, scope: str, workspace_id: str) -> str:
    """Keep legacy global IDs while isolating registrations in different workspaces."""
    owner = source if scope == "global" else f"workspace:{workspace_id}\n{source}"
    return registry.source_registry_id(owner, ref)


def validate_configuration(values: Mapping[str, Any]) -> dict[str, str]:
    """Validate source locations and Git selectors on the connected CLIO host."""
    result = {
        key: str(values.get(key) or "").strip()
        for key in ("name", "source", "ref", "pinned_commit", "working_checkout")
    }
    source = result["source"]
    if not source or any(char in source for char in "\r\n\x00") or source.startswith("-"):
        raise ValueError("A repository URL or absolute folder path is required")
    location = Path(source).expanduser()
    remote = "://" in source or bool(re.match(r"^[^/\\:]+@[^:]+:", source))
    if remote:
        parsed = urlsplit(source) if "://" in source else None
        if parsed and (parsed.password or (parsed.username and parsed.scheme != "ssh")):
            raise ValueError(
                "Use this CLIO host's Git authentication; do not put credentials in URLs"
            )
        if parsed and (parsed.query or parsed.fragment):
            raise ValueError("Use a repository URL without query parameters or fragments")
    elif not location.is_absolute() or not location.is_dir():
        raise ValueError("The marketplace folder must be an existing absolute path on this CLIO")
    if result["ref"]:
        checked = subprocess.run(
            ["git", "check-ref-format", "--branch", result["ref"]],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if checked.returncode or result["ref"].startswith("-"):
            raise ValueError("Enter a valid Git branch or tag")
    if result["pinned_commit"] and not re.fullmatch(r"[0-9a-fA-F]{40}", result["pinned_commit"]):
        raise ValueError("A pinned revision must be a complete 40-character Git commit")
    result["pinned_commit"] = result["pinned_commit"].lower()
    if result["working_checkout"]:
        checkout = Path(result["working_checkout"]).expanduser()
        if not checkout.is_absolute() or not checkout.is_dir():
            raise ValueError(
                "The working checkout must be an existing absolute folder on this CLIO"
            )
    return result


def update_configuration(source_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Save a compare-and-swap configuration edit without replacing installed files."""
    allowed = {"name", "source", "ref", "pinned_commit", "working_checkout", "expected_updated_at"}
    if set(request) - allowed:
        raise ValueError(
            "Source identity and installation scope cannot be edited; add a registration"
        )
    with BLUEPRINT_MUTATION_LOCK, registry._SOURCE_REGISTRY_LOCK:
        row = registered_source(source_id)
        if not row:
            raise FileNotFoundError("Marketplace registration not found")
        if not request.get("expected_updated_at") or request["expected_updated_at"] != row.get(
            "updated_at"
        ):
            raise SourceConfigurationConflict(
                "Marketplace changed; review its current configuration and retry"
            )
        values = validate_configuration({**row, **request})
        runtime_changed = any(
            values[key] != str(row.get(key) or "") for key in ("source", "ref", "pinned_commit")
        )
        updated = {**row, **values, "updated_at": datetime.now(UTC).isoformat()}
        if runtime_changed:
            updated.update(reload_required=True, available_blueprints=[])
        rows = [
            updated if item["id"] == source_id else item
            for item in registry.load_agent_blueprint_sources()
        ]
        registry.save_agent_blueprint_sources(rows)
        return updated


def require_unchanged(prepared: Mapping[str, Any]) -> None:
    """Refuse a stale Reload after a concurrent edit or removal."""
    current = registered_source(str(prepared["id"]))
    if not current or current.get("updated_at") != prepared.get("updated_at"):
        raise SourceConfigurationConflict("Marketplace changed while Reload was preparing; retry")


def configured_install(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Use current registered selectors for an explicit Reload, preserving ownership."""
    row = registered_source(str(metadata.get("source_id") or ""))
    if not row:
        return dict(metadata)
    return {
        **metadata,
        **{key: row.get(key, "") for key in ("source", "ref", "pinned_commit", "working_checkout")},
        "source_id": row["id"],
        "allow_pin_change": True,
    }
