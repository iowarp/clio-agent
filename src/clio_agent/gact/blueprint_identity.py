"""Marketplace-owned blueprint identities and unambiguous legacy resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.agent_blueprints import AgentBlueprintDefinition


class AmbiguousBlueprintError(ValueError):
    """A legacy id refers to more than one marketplace or installation scope."""


def identity_fields(blueprint: AgentBlueprintDefinition) -> dict[str, str]:
    """Identify an installed snapshot independently of its legacy directory name."""
    from clio_agent.gact.agent_blueprint_sources import source_registry_id

    install = blueprint.metadata.get("install") or {}
    source = str(install.get("source") or blueprint.root.resolve())
    source_id = str(
        install.get("source_id") or source_registry_id(source, str(install.get("ref") or ""))
    )
    scope = str(install.get("scope") or blueprint.scope)
    return {
        "identity": f"{scope}::{source_id}::{blueprint.id}",
        "source_id": source_id,
        "blueprint_id": blueprint.id,
    }


def select_blueprint(
    rows: Iterable[AgentBlueprintDefinition], identifier: str
) -> AgentBlueprintDefinition | None:
    """Resolve a qualified identity or migrate a unique legacy id; never guess."""
    matches = [row for row in rows if identifier in {row.id, identity_fields(row)["identity"]}]
    if len(matches) > 1:
        choices = ", ".join(identity_fields(row)["identity"] for row in matches)
        raise AmbiguousBlueprintError(
            f"Blueprint {identifier!r} is ambiguous; select one of: {choices}"
        )
    return matches[0] if matches else None


def installed_root(install_root: Path, identifier: str) -> Path:
    """Resolve within one install scope, rejecting traversal and ambiguous names."""
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root

    rows = (
        [
            parse_agent_blueprint_root(path, scope="install")
            for path in install_root.iterdir()
            if path.is_dir() and (path / "AGENT.md").is_file()
        ]
        if install_root.is_dir()
        else []
    )
    selected = select_blueprint(rows, identifier)
    if selected is None:
        raise FileNotFoundError(f"installed agent blueprint not found: {identifier}")
    selected.root.resolve().relative_to(install_root.resolve())
    return selected.root


def install_destination(install_root: Path, blueprint_id: str, metadata: Mapping[str, Any]) -> Path:
    """Retain an unambiguous legacy location; isolate same-named foreign sources."""
    from clio_agent.gact.agent_blueprint_sources import source_registry_id
    from clio_agent.gact.agent_blueprints import parse_agent_blueprint_root, read_install_metadata

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", blueprint_id) or blueprint_id in {".", ".."}:
        raise ValueError("invalid blueprint id")
    source_id = str(
        metadata.get("source_id")
        or source_registry_id(str(metadata["source"]), str(metadata.get("ref") or ""))
    )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise ValueError("invalid marketplace source id")
    matching = []
    for path in install_root.iterdir():
        if not path.is_dir() or not (path / "AGENT.md").is_file():
            continue
        previous = read_install_metadata(path)
        if (
            previous.get("source_id") == source_id
            or (
                (not previous.get("source_id") or not metadata.get("source_id"))
                and previous.get("source") == metadata.get("source")
                and previous.get("ref", "") in ("", metadata.get("ref", ""))
            )
        ) and parse_agent_blueprint_root(path, scope="install").id == blueprint_id:
            matching.append(path)
    if len(matching) > 1:
        raise AmbiguousBlueprintError("Multiple installed copies have the same source identity")
    if matching:
        destination = matching[0]
    else:
        destination = install_root / blueprint_id
        if destination.exists():
            destination = install_root / f"{source_id}--{blueprint_id}"
    destination.resolve().relative_to(install_root.resolve())
    return destination


def source_tombstones(ids: set[str], source: str, ref: str, scope: str) -> set[str]:
    """Select one marketplace's removals, including pre-identity ledger entries."""
    from clio_agent.gact.agent_blueprint_sources import source_registry_id

    prefixes = {f"{scope}::{source_registry_id(source, value)}::" for value in (ref, "")}
    return {
        item.rsplit("::", 1)[-1]
        for item in ids
        if "::" not in item or any(item.startswith(prefix) for prefix in prefixes)
    }
