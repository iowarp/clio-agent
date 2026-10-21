"""One entry point for installed plugins, agents and session resolutions.

The server, the ``clio-agent plugin`` CLI and the pytest fixture all resolve
through :class:`PluginService`, so a plugin that passes ``plugin check``
resolves the same way in a session.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.plugins.records import Diagnostic, PluginRecord
from clio_agent.plugins.resolve import (
    NO_DELTA,
    Catalog,
    Delta,
    Resolution,
    ResolutionError,
    resolve_agent,
    resolve_subagent,
)
from clio_agent.plugins.store import (
    BUILTIN_MARKETPLACE,
    USER_MARKETPLACE,
    PluginStore,
    StoreError,
    UserAgent,
)
from clio_agent.sdk.agent import AgentSpec
from clio_agent.sdk.versions import PluginRef

logger = logging.getLogger(__name__)

DEFAULT_MARKETPLACE = "clio"
DEFAULT_AGENT = "clio/standard"
DEFAULT_MARKETPLACE_URL = "https://github.com/iowarp/clio-agent-marketplace.git"
_SUBMODULE = Path(__file__).resolve().parents[3] / "external" / "clio-agent-marketplace"


class AgentNotFound(LookupError):
    """No installed agent answers to a reference."""

    def __init__(self, ref: str, reason: str = "agent_not_installed") -> None:
        super().__init__(f"agent {ref!r} is not installed")
        self.ref = ref
        self.reason = reason


@dataclass(frozen=True)
class AgentEntry:
    """An installed agent as the + menu and routes list it."""

    ref: str
    marketplace: str
    spec: AgentSpec
    user_agent: UserAgent | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ref": self.ref,
            "id": self.user_agent.id if self.user_agent else self.spec.id,
            "name": self.user_agent.name if self.user_agent else self.spec.name,
            "description": (self.user_agent.description if self.user_agent else "")
            or self.spec.description,
            "role": self.spec.role,
            "version": self.spec.version,
            "marketplace": self.marketplace,
            "flow": self.spec.flow,
            "plugins": [selection.to_dict() for selection in self.spec.plugins],
            "spec_checksum": self.spec.checksum,
            "kind": "user" if self.user_agent else "agent",
        }
        if self.user_agent:
            data["extends"] = self.user_agent.extends
            data["delta"] = {
                "add": list(self.user_agent.add),
                "remove": list(self.user_agent.remove),
            }
        return data


def plugin_store_root() -> Path:
    """``<config>/plugins``; ``CLIO_PLUGIN_STORE`` overrides it (tests, isolated runs)."""

    override = os.environ.get("CLIO_PLUGIN_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

    return paths.user_config_dir() / "plugins"


def default_marketplace_source() -> str:
    """The shipped default marketplace: the checkout's submodule, else the pinned URL."""

    configured = os.environ.get("CLIO_DEFAULT_MARKETPLACE", "").strip()
    if configured:
        return configured
    if (_SUBMODULE / "ai.iowarp.clio" / "marketplace.json").is_file():
        return str(_SUBMODULE)
    return DEFAULT_MARKETPLACE_URL


def native_tool_names() -> frozenset[str]:
    """Every CLIO built-in tool a plugin may claim with ``native_tool``."""

    from clio_agent.gact.agents.native_tool_registry import NATIVE_TOOL_NAMES  # noqa: PLC0415

    return NATIVE_TOOL_NAMES


def ui_catalog_text(resolution: Resolution) -> str:
    """The generated half of ``core.rendering``: the components the UI can draw."""

    from clio_agent.gact.a2ui_catalogs.plugin import catalog_components  # noqa: PLC0415

    lines = []
    for row in resolution.catalogs:
        components, error = catalog_components(row["id"], row.get("path") or "")
        if error:
            lines.append(f"- {row['id']}: unavailable ({error})")
        else:
            lines.append(f"- {row['id']}: {', '.join(components)}")
    if not lines:
        return "No A2UI component catalog is available in this session."
    return "A2UI components this session can display, by catalog:\n" + "\n".join(lines)


class PluginService:
    """Installed plugins and agents, resolution and pinned session resolutions."""

    def __init__(
        self,
        store: PluginStore | None = None,
        *,
        native_tools: frozenset[str] | None = None,
        default_marketplace: bool | None = None,
    ) -> None:
        """``default_marketplace`` installs the shipped marketplace on first use.

        It defaults to on for the user's own store and off for an explicit one.
        """

        self.store = store or PluginStore(plugin_store_root())
        self._native_tools = native_tools
        self._install_default = store is None if default_marketplace is None else default_marketplace
        self._ready_lock = threading.Lock()
        self._ready = False
        self.ready_error: dict[str, Any] | None = None

    def ready(self) -> None:
        """Install or refresh the default marketplace once (idempotent, thread-safe).

        A failure is recorded on ``ready_error`` with a typed reason and logged;
        agents from other marketplaces stay usable.
        """

        if self._ready or not self._install_default:
            return
        with self._ready_lock:
            if self._ready:
                return
            try:
                self.ensure_default_marketplace()
                self.ready_error = None
            except Exception as exc:  # noqa: BLE001 - recorded as a typed reason below
                self.ready_error = {
                    "reason": "default_marketplace_unavailable",
                    "source": default_marketplace_source(),
                    "message": str(exc),
                }
                logger.error(
                    "default marketplace install failed reason=default_marketplace_unavailable "
                    "source=%s error=%r",
                    default_marketplace_source(),
                    exc,
                )
            self._ready = True

    # ---------------------------------------------------------------- catalog
    def catalog(self, extra: list[PluginRecord] | None = None) -> Catalog:
        self.ready()
        records = self.store.records()
        if extra:
            replaced = {(r.marketplace, r.id) for r in extra}
            records = [r for r in records if (r.marketplace, r.id) not in replaced] + list(extra)
        extra_ids = {(r.marketplace, r.id) for r in extra or []}
        return Catalog(
            records=records,
            trusted=lambda record: (record.marketplace, record.id) in extra_ids
            or self.store.trusted(record),
            native_tools=self._native_tools
            if self._native_tools is not None
            else native_tool_names(),
            default_marketplace=DEFAULT_MARKETPLACE,
            generators={"ui_catalog": ui_catalog_text},
        )

    def ensure_default_marketplace(self) -> dict[str, Any] | None:
        """Install or refresh the shipped default marketplace. Returns the summary if it ran."""

        source = default_marketplace_source()
        installed = self.store.marketplaces().get(DEFAULT_MARKETPLACE)
        if installed and installed.get("source") == source:
            from clio_agent.plugins.loader import tree_digests  # noqa: PLC0415

            if Path(source).is_dir():
                digest = "".join(tree_digests(Path(source)))
                snapshot = Path(installed.get("snapshot") or "")
                if snapshot.is_dir() and "".join(tree_digests(snapshot)) == digest:
                    return None
            else:
                return None
        return self.store.install(source, name=DEFAULT_MARKETPLACE, default=True)

    # ----------------------------------------------------------------- agents
    def agents(self) -> list[AgentEntry]:
        self.ready()
        specs = self.store.agent_specs()
        out = [AgentEntry(ref, ref.split("/", 1)[0], spec) for ref, spec in sorted(specs.items())]
        for ref, user_agent in sorted(self.store.user_agents().items()):
            try:
                parent = self.lookup(user_agent.extends)
            except AgentNotFound:
                continue
            out.append(AgentEntry(ref, USER_MARKETPLACE, parent.spec, user_agent))
        return out

    def lookup(self, ref: str) -> AgentEntry:
        """Find an agent by ``market/id``, ``id`` or ``user/id``, honoring ``@range``."""

        self.ready()
        text = str(ref or "").strip() or DEFAULT_AGENT
        name, _, version_range = text.partition("@")
        if name.startswith(f"{USER_MARKETPLACE}/"):
            user_agent = self.store.user_agents().get(name)
            if user_agent is None:
                raise AgentNotFound(text)
            parent = self.lookup(user_agent.extends)
            return AgentEntry(name, USER_MARKETPLACE, parent.spec, user_agent)
        specs = self.store.agent_specs()
        if "/" in name:
            matches = [(name, specs[name])] if name in specs else []
        else:
            matches = [(key, spec) for key, spec in specs.items() if key.split("/", 1)[1] == name]
            if len(matches) > 1:
                preferred = [m for m in matches if m[0].startswith(f"{DEFAULT_MARKETPLACE}/")]
                if len(preferred) != 1:
                    raise AgentNotFound(text, reason="agent_ambiguous")
                matches = preferred
        if not matches:
            raise AgentNotFound(text)
        key, spec = matches[0]
        if version_range and not PluginRef(spec.id, version_range).accepts(spec.version):
            raise AgentNotFound(text, reason="agent_version_mismatch")
        return AgentEntry(key, key.split("/", 1)[0], spec)

    def _user_delta(self, entry: AgentEntry) -> tuple[Delta, AgentEntry]:
        """Flatten a chain of user agents into one delta over the shipped root agent."""

        if entry.user_agent is None:
            return Delta(), entry
        parent = self.lookup(entry.user_agent.extends)
        delta, root = self._user_delta(parent)
        own = Delta(tuple(entry.user_agent.add), tuple(entry.user_agent.remove))
        return delta.then(own), root

    # ------------------------------------------------------------- resolution
    def resolve(
        self,
        ref: str,
        *,
        delta: Delta = NO_DELTA,
        extra: list[PluginRecord] | None = None,
        pin: bool = True,
    ) -> Resolution:
        """Resolve an agent (with a session delta) and pin the result."""

        entry = self.lookup(ref)
        user_delta, root = self._user_delta(entry)
        identity: dict[str, str] = {}
        if entry.user_agent is not None:
            identity = {
                "id": entry.user_agent.id,
                "name": entry.user_agent.name,
                "description": entry.user_agent.description,
                "parent": root.ref,
            }
        resolution = resolve_agent(
            self.catalog(extra),
            root.spec,
            marketplace=root.marketplace,
            delta=user_delta.then(delta),
            identity=identity,
        )
        if entry.user_agent is not None:
            resolution.agent = {
                **resolution.agent,
                "ref": entry.ref,
                "kind": "user",
                "extends": entry.user_agent.extends,
            }
        if pin:
            self.store.save_resolution(resolution.digest, resolution.to_dict())
        return resolution

    def resolve_subagent(
        self, parent: Resolution, subagent_id: str, *, pin: bool = True
    ) -> Resolution:
        resolution = resolve_subagent(self.catalog(), parent, subagent_id)
        if pin:
            self.store.save_resolution(resolution.digest, resolution.to_dict())
        return resolution

    def pinned(self, digest: str) -> Resolution | None:
        data = self.store.load_resolution(digest)
        return Resolution.from_dict(data) if data is not None else None

    def preview_removal(
        self, ref: str, plugin_id: str, *, delta: Delta = NO_DELTA
    ) -> dict[str, Any]:
        """What else leaves if ``plugin_id`` is removed: the + menu's cascade preview."""

        before = self.resolve(ref, delta=delta, pin=False)
        after = self.resolve(ref, delta=delta.then(Delta(remove=(plugin_id,))), pin=False)
        kept = set(after.plugin_ids)
        leaving = [row for row in before.plugins if row["id"] not in kept]
        return {
            "plugin": plugin_id,
            "removes": [
                {
                    "id": row["id"],
                    "reason": next(
                        (r for r in after.removed if r["id"] == row["id"]),
                        {"reason": "removed", "because": ""},
                    ),
                }
                for row in leaving
            ],
            "tools_removed": sorted(set(before.tool_names) - set(after.tool_names)),
        }

    # ------------------------------------------------------------ user agents
    def save_user_agent(
        self,
        agent_id: str,
        *,
        extends: str,
        add: list[str],
        remove: list[str],
        name: str = "",
        description: str = "",
    ) -> AgentEntry:
        parent = self.lookup(extends)
        if parent.user_agent is None and "@" not in extends:
            major = parent.spec.version.split(".", 1)[0]
            extends = f"{parent.ref}@^{major}"
        user_agent = UserAgent(
            id=agent_id,
            name=name or agent_id,
            extends=extends,
            add=tuple(add),
            remove=tuple(remove),
            description=description,
        )
        self.store.save_user_agent(user_agent)
        entry = self.lookup(f"{USER_MARKETPLACE}/{agent_id}")
        self.resolve(entry.ref, pin=False)
        return entry


def resolution_error_payload(exc: ResolutionError) -> dict[str, Any]:
    return {
        "reason": "agent_resolution_failed",
        "errors": [row.to_dict() for row in exc.diagnostics],
        "message": str(exc),
    }


def diagnostics_payload(rows: list[Diagnostic] | list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [row.to_dict() if isinstance(row, Diagnostic) else dict(row) for row in rows]


__all__ = [
    "BUILTIN_MARKETPLACE",
    "DEFAULT_AGENT",
    "DEFAULT_MARKETPLACE",
    "AgentEntry",
    "AgentNotFound",
    "PluginService",
    "StoreError",
    "plugin_store_root",
]
