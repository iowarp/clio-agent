"""Read a marketplace into plugin records and agent specs.

Two formats:

* **Native** -- ``plugins/<id>/plugin.json`` Agent Plugins packages and Python
  agent classes in ``agents/*.py``, with ``ai.iowarp.clio/marketplace.json``
  naming the marketplace.
* **Claude Code** -- ``.claude-plugin/marketplace.json`` listing plugins whose
  ``source`` is a relative path or a git repository pinned by commit, plus
  ``.claude-plugin/federated-marketplaces.json`` naming other marketplaces
  pinned by commit. Client-specific index files outside that format (such as
  ``.clio-kit/catalogue.json``) are not read.

Agent classes are Python, so they are imported only for a trusted marketplace.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.plugins.loader import (
    PluginImportError,
    PluginLoadError,
    import_module_typed,
    load_plugin,
)
from clio_agent.plugins.manifest import within
from clio_agent.plugins.records import Diagnostic, PluginRecord
from clio_agent.sdk.agent import Agent, AgentDefinitionError, AgentSpec

NATIVE_INDEX = Path("ai.iowarp.clio") / "marketplace.json"
CLAUDE_INDEX = Path(".claude-plugin") / "marketplace.json"
CLAUDE_FEDERATION = Path(".claude-plugin") / "federated-marketplaces.json"


@dataclass(frozen=True)
class GitPin:
    """A plugin or marketplace that lives in another git repository at a commit."""

    url: str
    commit: str
    path: str = ""
    name: str = ""
    entry: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketplaceContents:
    name: str
    format: str
    description: str = ""
    plugins: list[PluginRecord] = field(default_factory=list)
    agents: list[AgentSpec] = field(default_factory=list)
    git_plugins: list[GitPin] = field(default_factory=list)
    federated: list[GitPin] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def detect_format(root: Path) -> str:
    if (root / CLAUDE_INDEX).is_file():
        return "claude-code"
    if (root / NATIVE_INDEX).is_file() or (root / "plugins").is_dir():
        return "agent-plugins"
    if (root / "plugin.json").is_file():
        return "plugin"
    if (root / ".claude-plugin" / "plugin.json").is_file():
        return "claude-code-plugin"
    return ""


def _github_url(repo: str) -> str:
    return f"https://github.com/{repo.strip('/')}.git"


def _pin_from_source(source: Any, entry: dict[str, Any]) -> GitPin | None:
    if not isinstance(source, dict):
        return None
    kind = source.get("source")
    commit = str(source.get("sha") or source.get("commit") or "")
    if kind == "github":
        return GitPin(
            _github_url(str(source.get("repo") or "")),
            commit,
            "",
            str(entry.get("name") or ""),
            entry,
        )
    if kind in ("git-subdir", "git", "url"):
        return GitPin(
            str(source.get("url") or ""),
            commit,
            str(source.get("path") or ""),
            str(entry.get("name") or ""),
            entry,
        )
    return None


def read_claude_marketplace(root: Path, name_hint: str = "") -> MarketplaceContents:
    data = json.loads((root / CLAUDE_INDEX).read_text(encoding="utf-8"))
    name = str(data.get("name") or name_hint or root.name)
    contents = MarketplaceContents(
        name=name,
        format="claude-code",
        description=str((data.get("metadata") or {}).get("description") or ""),
    )
    for entry in data.get("plugins") or []:
        plugin_name = str(entry.get("name") or "")
        source = entry.get("source")
        if isinstance(source, str):
            path = (root / source).resolve()
            if not within(root, path) or not path.is_dir():
                contents.diagnostics.append(
                    Diagnostic(
                        "error",
                        "plugin_source_missing",
                        f"source {source!r} is not a folder in the marketplace",
                        plugin_name,
                    )
                )
                continue
            contents.plugins.append(import_claude_plugin(path, entry=entry, marketplace=name))
            continue
        pin = _pin_from_source(source, entry)
        if pin is None or not pin.url:
            contents.diagnostics.append(
                Diagnostic(
                    "error",
                    "plugin_source_unsupported",
                    f"unsupported plugin source {source!r}",
                    plugin_name,
                )
            )
            continue
        if not pin.commit:
            contents.diagnostics.append(
                Diagnostic(
                    "error",
                    "plugin_source_unpinned",
                    f"git source {pin.url} is not pinned by commit; it is not followed",
                    plugin_name,
                )
            )
            continue
        contents.git_plugins.append(pin)
    federation = root / CLAUDE_FEDERATION
    if federation.is_file():
        try:
            fed = json.loads(federation.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            contents.diagnostics.append(
                Diagnostic("error", "federation_invalid", f"{CLAUDE_FEDERATION}: {exc}", name)
            )
            fed = {}
        for row in fed.get("marketplaces") or []:
            pin = _pin_from_source(row.get("source"), row)
            if pin is None or not pin.commit:
                contents.diagnostics.append(
                    Diagnostic(
                        "error",
                        "federation_unpinned",
                        f"federated marketplace {row.get('name')!r} is not pinned by commit; it is not followed",
                        name,
                    )
                )
                continue
            contents.federated.append(pin)
    return contents


def _agent_package(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"_clio_marketplace_{digest}"


def load_agent_specs(root: Path, marketplace: str) -> tuple[list[AgentSpec], list[Diagnostic]]:
    """Import ``agents/*.py`` once and freeze every agent class to its spec."""

    agents_dir = root / "agents"
    if not agents_dir.is_dir():
        return [], []
    package = _agent_package(root)
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(root)]
        sys.modules[package] = module
        agents_pkg = types.ModuleType(f"{package}.agents")
        agents_pkg.__path__ = [str(agents_dir)]
        sys.modules[f"{package}.agents"] = agents_pkg
    specs: dict[str, AgentSpec] = {}
    diagnostics: list[Diagnostic] = []
    for path in sorted(agents_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            module = import_module_typed(f"{package}.agents.{path.stem}", f"agents/{path.name}")
        except PluginImportError as exc:
            diagnostics.append(Diagnostic("error", "agent_import_failed", str(exc), marketplace))
            continue
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, Agent)
                and value is not Agent
                and value.__module__ == module.__name__
                and value.__dict__.get("id")
            ):
                try:
                    spec = value.spec()
                except AgentDefinitionError as exc:
                    diagnostics.append(
                        Diagnostic(
                            "error", "agent_invalid", f"{value.__name__}: {exc}", marketplace
                        )
                    )
                    continue
                if spec.id in specs:
                    diagnostics.append(
                        Diagnostic(
                            "error",
                            "agent_duplicate",
                            f"agent id {spec.id!r} is defined twice",
                            marketplace,
                        )
                    )
                    continue
                specs[spec.id] = spec
    return list(specs.values()), diagnostics


def read_native_marketplace(
    root: Path, *, trusted: bool, name_hint: str = ""
) -> MarketplaceContents:
    index: dict[str, Any] = {}
    if (root / NATIVE_INDEX).is_file():
        index = json.loads((root / NATIVE_INDEX).read_text(encoding="utf-8"))
    name = str(index.get("name") or name_hint or root.name)
    contents = MarketplaceContents(
        name=name, format="agent-plugins", description=str(index.get("description") or "")
    )
    plugins_dir = root / "plugins"
    for child in sorted(plugins_dir.iterdir()) if plugins_dir.is_dir() else []:
        if not (child / "plugin.json").is_file():
            continue
        try:
            contents.plugins.append(load_plugin(child, marketplace=name, trusted=trusted))
        except PluginLoadError as exc:
            contents.diagnostics.append(Diagnostic("error", "plugin_invalid", str(exc), child.name))
    if trusted:
        specs, diagnostics = load_agent_specs(root, name)
        contents.agents.extend(specs)
        contents.diagnostics.extend(diagnostics)
    elif (root / "agents").is_dir():
        contents.diagnostics.append(
            Diagnostic(
                "warning",
                "marketplace_untrusted",
                "agent classes are imported after the trust step",
                name,
            )
        )
    return contents


def read_marketplace(root: Path, *, trusted: bool, name_hint: str = "") -> MarketplaceContents:
    """Read any supported marketplace (or single plugin) at ``root``."""

    kind = detect_format(root)
    if kind == "claude-code":
        return read_claude_marketplace(root, name_hint)
    if kind == "agent-plugins":
        return read_native_marketplace(root, trusted=trusted, name_hint=name_hint)
    if kind == "plugin":
        record = load_plugin(root, marketplace=name_hint or root.name, trusted=trusted)
        return MarketplaceContents(
            name=name_hint or record.id, format="agent-plugins", plugins=[record]
        )
    if kind == "claude-code-plugin":
        record = import_claude_plugin(root, marketplace=name_hint or root.name)
        return MarketplaceContents(
            name=name_hint or record.id, format="claude-code", plugins=[record]
        )
    raise PluginLoadError(f"{root}: no marketplace or plugin found")
