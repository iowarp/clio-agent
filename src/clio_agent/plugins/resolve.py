"""Stage 2 resolution: one agent, for one user, at session start.

Given an agent's frozen spec and the installed plugin records:

1. expand packs to the full plugin set;
2. apply the user's delta (additions, then removals);
3. pull in hard requirements, and cascade the removal of a required plugin to
   every plugin that requires it;
4. check trust, validity, version ranges, collisions and the model
   requirement union;
5. order hooks child to parent, then by ``before``/``after``; a cycle fails;
6. compose the main prompt;
7. return a :class:`Resolution` the session pins.

Every failure is a typed :class:`~clio_agent.plugins.records.Diagnostic`; a
resolution with errors raises :class:`ResolutionError` naming each one.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clio_agent.plugins.compose import ComposedPrompt, compose
from clio_agent.plugins.records import Diagnostic, FragmentRecord, PluginRecord, SubagentRecord
from clio_agent.sdk.agent import CORE_PLUGIN, AgentSpec
from clio_agent.sdk.model import Prefers, Requires
from clio_agent.sdk.versions import PluginRef, VersionRangeError


class ResolutionError(ValueError):
    """Resolution failed; ``diagnostics`` names every error with its plugin."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self.diagnostics = [row for row in diagnostics if row.severity == "error"]
        super().__init__("; ".join(str(row) for row in self.diagnostics) or "resolution failed")


@dataclass(frozen=True)
class Delta:
    """Plugins a user adds or removes relative to the parent agent."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    def then(self, other: "Delta") -> "Delta":
        """This delta followed by ``other`` (a later removal wins over an earlier add)."""

        add = [ref for ref in self.add if PluginRef.parse(ref).id not in _ids(other.remove)]
        add += [ref for ref in other.add if PluginRef.parse(ref).id not in _ids(add)]
        remove = [ref for ref in self.remove if PluginRef.parse(ref).id not in _ids(other.add)]
        remove += [ref for ref in other.remove if ref not in remove]
        return Delta(tuple(add), tuple(remove))

    def to_dict(self) -> dict[str, list[str]]:
        return {"add": list(self.add), "remove": list(self.remove)}


NO_DELTA = Delta()


def _ids(refs: Iterable[str]) -> set[str]:
    return {PluginRef.parse(ref).id for ref in refs}


@dataclass
class Catalog:
    """What resolution reads: installed records, trust and the native tool set."""

    records: list[PluginRecord]
    trusted: Callable[[PluginRecord], bool]
    native_tools: frozenset[str]
    default_marketplace: str = ""
    generators: Mapping[str, Callable[["Resolution"], str]] = field(default_factory=dict)

    def candidates(self, plugin_id: str) -> list[PluginRecord]:
        return [record for record in self.records if record.id == plugin_id]


@dataclass
class Entry:
    record: PluginRecord
    source: str  # core | agent | pack | user | required
    via: str
    depth: int
    order: int


@dataclass
class Resolution:
    """The pinned result of stage 2. Plain data via :meth:`to_dict`."""

    agent: dict[str, Any]
    plugins: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    mcp_servers: dict[str, dict[str, Any]]
    skills: list[dict[str, Any]]
    hooks: list[dict[str, Any]]
    subagents: list[dict[str, Any]]
    flows: list[dict[str, Any]]
    catalogs: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    prompt: dict[str, Any]
    model: dict[str, Any]
    workflow_state: dict[str, Any] | None
    delta: dict[str, list[str]]
    diagnostics: list[dict[str, Any]]
    digest: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "plugins": self.plugins,
            "removed": self.removed,
            "tools": self.tools,
            "mcp_servers": self.mcp_servers,
            "skills": self.skills,
            "hooks": self.hooks,
            "subagents": self.subagents,
            "flows": self.flows,
            "catalogs": self.catalogs,
            "commands": self.commands,
            "prompt": self.prompt,
            "model": self.model,
            "workflow_state": self.workflow_state,
            "delta": self.delta,
            "diagnostics": self.diagnostics,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "digest": self.digest}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Resolution":
        values = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        return cls(**values)

    @property
    def plugin_ids(self) -> list[str]:
        return [row["id"] for row in self.plugins]

    @property
    def tool_names(self) -> list[str]:
        return [row["name"] for row in self.tools]

    @property
    def prompt_text(self) -> str:
        return str(self.prompt.get("text") or "")


class _Builder:
    def __init__(self, catalog: Catalog, home_marketplace: str) -> None:
        self.catalog = catalog
        self.home = home_marketplace
        self.entries: dict[str, Entry] = {}
        self.diagnostics: list[Diagnostic] = []
        self.counter = 0

    def error(self, code: str, message: str, plugin: str = "") -> None:
        self.diagnostics.append(Diagnostic("error", code, message, plugin))

    def pick(self, ref: PluginRef, wanted_by: str) -> PluginRecord | None:
        candidates = [r for r in self.catalog.candidates(ref.id) if r.kind in ("plugin", "pack")]
        if not candidates:
            self.error(
                "plugin_not_installed",
                f"{ref.id!r} is not installed (needed by {wanted_by})",
                ref.id,
            )
            return None
        matching: list[PluginRecord] = []
        for record in candidates:
            try:
                if ref.accepts(record.version):
                    matching.append(record)
            except VersionRangeError:
                continue
        if not matching:
            versions = ", ".join(sorted({r.version for r in candidates}))
            self.error(
                "plugin_version_mismatch",
                f"{wanted_by} needs {ref}, but installed versions are {versions}",
                ref.id,
            )
            return None
        if len(matching) == 1:
            return matching[0]
        for preferred in (self.home, "clio-builtin", self.catalog.default_marketplace):
            chosen = [r for r in matching if r.marketplace == preferred]
            if len(chosen) == 1:
                return chosen[0]
        markets = ", ".join(sorted(r.marketplace for r in matching))
        self.error(
            "plugin_ambiguous",
            f"{ref.id!r} is provided by more than one marketplace ({markets})",
            ref.id,
        )
        return None

    def add(self, ref: PluginRef, source: str, via: str, depth: int, *, wanted_by: str) -> None:
        if ref.id in self.entries:
            existing = self.entries[ref.id]
            if not ref.accepts(existing.record.version):
                self.error(
                    "plugin_version_mismatch",
                    f"{wanted_by} needs {ref}, but {existing.record.version} is resolved",
                    ref.id,
                )
            return
        record = self.pick(ref, wanted_by)
        if record is None:
            return
        self.counter += 1
        if ref.id == CORE_PLUGIN:
            source, via = "core", "core"
        self.entries[ref.id] = Entry(record, source, via, depth, self.counter)
        if record.kind == "pack":
            for member in record.members:
                self.add(
                    PluginRef.parse(member), "pack", record.id, depth, wanted_by=f"pack {record.id}"
                )


def _expand_remove(builder: _Builder, removals: set[str]) -> dict[str, dict[str, str]]:
    """Apply removals and the requirement cascade. Returns removed id -> reason."""

    removed: dict[str, dict[str, str]] = {}
    if CORE_PLUGIN in removals:
        builder.error("core_not_removable", "every agent carries the core plugin", CORE_PLUGIN)
        removals = removals - {CORE_PLUGIN}
    for plugin_id in sorted(removals):
        entry = builder.entries.get(plugin_id)
        if entry is None:
            continue
        removed[plugin_id] = {"id": plugin_id, "reason": "removed", "because": ""}
        if entry.record.kind == "pack":
            for member, member_entry in list(builder.entries.items()):
                if member_entry.source == "pack" and member_entry.via == plugin_id:
                    removed.setdefault(
                        member, {"id": member, "reason": "removed", "because": plugin_id}
                    )
    for plugin_id in removed:
        builder.entries.pop(plugin_id, None)
    # A requirement that was removed (whether or not it had been pulled in yet)
    # takes its dependents with it; any other missing requirement is pulled in.
    gone = set(removed) | removals
    changed = True
    while changed:
        changed = False
        for plugin_id, entry in list(builder.entries.items()):
            for raw in entry.record.requires:
                ref = PluginRef.parse(raw)
                if ref.id in gone:
                    removed[plugin_id] = {"id": plugin_id, "reason": "cascade", "because": ref.id}
                    builder.entries.pop(plugin_id, None)
                    gone.add(plugin_id)
                    changed = True
                    break
                if ref.id not in builder.entries:
                    builder.add(
                        ref, "required", plugin_id, entry.depth, wanted_by=f"plugin {plugin_id}"
                    )
                    changed = True
    return removed


def _order_hooks(entries: list[Entry], builder: _Builder) -> list[dict[str, Any]]:
    """Child to parent (deepest class level first), then ``before``/``after``."""

    hooks: list[tuple[int, dict[str, Any]]] = []
    ranked = sorted(entries, key=lambda e: (-e.depth, e.order))
    for entry in ranked:
        for hook in entry.record.hooks:
            row = {**_hook_dict(hook), "plugin": entry.record.id, "root": entry.record.root}
            hooks.append((len(hooks), row))
    ids = {row["id"]: index for index, row in hooks}
    edges: dict[int, set[int]] = {index: set() for index, _ in hooks}
    for index, row in hooks:
        for target in row["before"]:
            if target in ids:
                edges[index].add(ids[target])
        for target in row["after"]:
            if target in ids:
                edges[ids[target]].add(index)
    indegree = dict.fromkeys(edges, 0)
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1
    queue = [index for index, degree in indegree.items() if degree == 0]
    heapq.heapify(queue)
    ordered: list[int] = []
    while queue:
        index = heapq.heappop(queue)
        ordered.append(index)
        for target in sorted(edges[index]):
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(queue, target)
    if len(ordered) != len(hooks):
        stuck = sorted(hooks[i][1]["id"] for i, degree in indegree.items() if degree > 0)
        builder.error(
            "hook_cycle",
            f"hook ordering has a cycle among {', '.join(stuck)}; fix their before/after",
            hooks[next(i for i, d in indegree.items() if d > 0)][1]["plugin"],
        )
        return [row for _, row in hooks]
    return [hooks[index][1] for index in ordered]


def _hook_dict(hook: Any) -> dict[str, Any]:
    return {
        "id": hook.id,
        "event": hook.event,
        "kind": hook.kind,
        "module": hook.module,
        "attr": hook.attr,
        "command": hook.command,
        "args": list(hook.args),
        "tools": hook.tools,
        "timeout_ms": hook.timeout_ms,
        "before": list(hook.before),
        "after": list(hook.after),
        "fail_closed": hook.fail_closed,
    }


def _provenance(entry: Entry) -> dict[str, str]:
    if entry.source == "core":
        return {"source": "core", "via": "core", "label": "core"}
    if entry.source == "pack":
        return {"source": "pack", "via": entry.via, "label": f"pack {entry.via}"}
    if entry.source == "required":
        return {"source": "required", "via": entry.via, "label": f"required by {entry.via}"}
    if entry.source == "user":
        return {"source": "user", "via": "user", "label": "user"}
    return {"source": "agent", "via": entry.via, "label": entry.via}


def _resolve_set(
    builder: _Builder,
    selections: Sequence[tuple[PluginRef, str, str, int]],
    delta: Delta,
    user_depth: int,
) -> dict[str, dict[str, str]]:
    for ref, source, via, depth in selections:
        builder.add(ref, source, via, depth, wanted_by=f"agent level {via}")
    for raw in delta.add:
        builder.add(PluginRef.parse(raw), "user", "user", user_depth, wanted_by="the user")
    removals = _ids(delta.remove)
    removed = _expand_remove(builder, removals)
    # A removal that targets a plugin the set never had is reported, not ignored.
    for plugin_id in removals - set(removed):
        builder.diagnostics.append(
            Diagnostic(
                "warning",
                "removal_not_present",
                f"{plugin_id!r} is not part of this agent",
                plugin_id,
            )
        )
    return removed


def _check_entries(builder: _Builder, entries: list[Entry]) -> None:
    for entry in entries:
        record = entry.record
        for row in record.errors:
            builder.diagnostics.append(row)
        if record.has_executable_parts and not builder.catalog.trusted(record):
            builder.error(
                "plugin_untrusted",
                f"{record.id} from {record.marketplace} has executable parts that have not passed "
                "the trust step (run: clio-agent plugin trust "
                f"{record.marketplace})",
                record.id,
            )


def _collect(builder: _Builder, entries: list[Entry]) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    tool_owner: dict[str, str] = {}
    servers: dict[str, dict[str, Any]] = {}
    skills: list[dict[str, Any]] = []
    skill_owner: dict[str, str] = {}
    subagents: list[tuple[str, SubagentRecord]] = []
    sub_owner: dict[str, str] = {}
    flows: list[dict[str, Any]] = []
    flow_owner: dict[str, str] = {}
    catalogs: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    workflow_state: tuple[str, dict[str, Any]] | None = None
    for entry in entries:
        record = entry.record
        for tool in record.tools:
            if tool.kind == "native" and tool.name not in builder.catalog.native_tools:
                builder.error(
                    "native_tool_unknown", f"CLIO has no built-in tool {tool.name!r}", record.id
                )
                continue
            if tool.name in tool_owner:
                builder.error(
                    "tool_collision",
                    f"tool {tool.name!r} is exposed by both {tool_owner[tool.name]} and {record.id}",
                    record.id,
                )
                continue
            tool_owner[tool.name] = record.id
            tools.append(
                {
                    "name": tool.name,
                    "plugin": record.id,
                    "kind": tool.kind,
                    "module": tool.module,
                    "attr": tool.attr,
                    "root": record.root,
                    "description": tool.description,
                    "read_only": tool.read_only,
                }
            )
        for name, spec in record.mcp_servers.items():
            if name in servers:
                builder.error(
                    "mcp_server_collision",
                    f"MCP server {name!r} is declared by both {servers[name]['plugin']} and {record.id}",
                    record.id,
                )
                continue
            servers[name] = {"plugin": record.id, "root": record.root, "spec": spec}
        for skill in record.skills:
            if skill.id in skill_owner:
                builder.error(
                    "skill_collision",
                    f"skill {skill.id!r} is provided by both {skill_owner[skill.id]} and {record.id}",
                    record.id,
                )
                continue
            skill_owner[skill.id] = record.id
            skills.append(
                {
                    "id": skill.id,
                    "plugin": record.id,
                    "path": skill.path,
                    "description": skill.description,
                }
            )
        for sub in record.subagents:
            if sub.id in sub_owner:
                builder.error(
                    "subagent_collision",
                    f"subagent {sub.id!r} is defined by both {sub_owner[sub.id]} and {record.id}",
                    record.id,
                )
                continue
            sub_owner[sub.id] = record.id
            subagents.append((record.id, sub))
        for flow in record.flows:
            if flow.id in flow_owner:
                builder.error(
                    "flow_collision",
                    f"flow {flow.id!r} is defined by both {flow_owner[flow.id]} and {record.id}",
                    record.id,
                )
                continue
            flow_owner[flow.id] = record.id
            flows.append(
                {
                    "id": flow.id,
                    "plugin": record.id,
                    "module": flow.module,
                    "attr": flow.attr,
                    "root": record.root,
                    "description": flow.description,
                }
            )
        for catalog in record.catalogs:
            catalogs.append({"id": catalog.id, "plugin": record.id, "path": catalog.path})
        for command in record.commands:
            commands.append(
                {
                    "id": command.id,
                    "plugin": record.id,
                    "path": command.path,
                    "description": command.description,
                }
            )
        if record.workflow_state is not None:
            if workflow_state is not None:
                builder.error(
                    "workflow_state_collision",
                    f"both {workflow_state[0]} and {record.id} declare a workflow_state vocabulary",
                    record.id,
                )
            else:
                workflow_state = (record.id, record.workflow_state)
    return {
        "tools": tools,
        "servers": servers,
        "skills": skills,
        "subagents": subagents,
        "flows": flows,
        "catalogs": catalogs,
        "commands": commands,
        "workflow_state": workflow_state[1] if workflow_state else None,
    }


def _model_union(
    agent_requires: Mapping[str, Any],
    agent_prefers: Mapping[str, Any],
    entries: list[Entry],
    agent_id: str,
) -> dict[str, Any]:
    union = Requires.from_dict(agent_requires)
    attribution: dict[str, list[str]] = {key: [f"agent:{agent_id}"] for key in union.to_dict()}
    for entry in entries:
        if not entry.record.model:
            continue
        need = Requires.from_dict(entry.record.model)
        union = union.union(need)
        for key in need.to_dict():
            attribution.setdefault(key, []).append(entry.record.id)
    return {
        "requires": union.to_dict(),
        "attribution": attribution,
        "prefers": Prefers.from_dict(agent_prefers).to_dict(),
    }


def _digest(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _finish(
    builder: _Builder,
    *,
    agent: dict[str, Any],
    identity_text: str,
    entries: list[Entry],
    removed: dict[str, dict[str, str]],
    requires: Mapping[str, Any],
    prefers: Mapping[str, Any],
    delta: Delta,
    tool_allowlist: Sequence[str] | None = None,
    flow_id: str = "react",
) -> Resolution:
    _check_entries(builder, entries)
    collected = _collect(builder, entries)
    tools = collected["tools"]
    if tool_allowlist is not None:
        available = {row["name"] for row in tools}
        server_names = set(collected["servers"])
        for name in tool_allowlist:
            namespace = name.split("_", 1)[0]
            if name not in available and namespace not in server_names:
                builder.error(
                    "subagent_tool_unavailable",
                    f"{agent['id']} allows tool {name!r}, but none of its plugins provides it",
                    agent["id"],
                )
        # The allowlist narrows what the subagent's plugins add; core's tools (task
        # controls, questions, skills) stay, since core is never removable (D11).
        allowed = set(tool_allowlist)
        tools = [row for row in tools if row["name"] in allowed or row.get("plugin") == "core"]
    flows = collected["flows"]
    if flow_id not in {row["id"] for row in flows}:
        builder.error(
            "flow_missing",
            f"{agent['id']} selects flow {flow_id!r}, which no plugin provides",
            agent["id"],
        )
    hooks = _order_hooks(entries, builder)
    plugin_ids = [entry.record.id for entry in entries]
    fragments: list[tuple[str, FragmentRecord]] = []
    for entry in entries:
        fragments.extend((entry.record.id, fragment) for fragment in entry.record.fragments)
    tool_names = [row["name"] for row in tools] + [f"{name}_" for name in collected["servers"]]
    resolution = Resolution(
        agent=agent,
        plugins=[
            {
                "id": entry.record.id,
                "version": entry.record.version,
                "kind": entry.record.kind,
                "format": entry.record.format,
                "marketplace": entry.record.marketplace,
                "description": entry.record.description,
                "root": entry.record.root,
                "provenance": _provenance(entry),
                "requires": list(entry.record.requires),
                "members": list(entry.record.members),
                "removable": entry.record.id != CORE_PLUGIN,
                "executable_digest": entry.record.executable_digest,
                "text_digest": entry.record.text_digest,
            }
            for entry in entries
        ],
        removed=list(removed.values()),
        tools=tools,
        mcp_servers=collected["servers"],
        skills=collected["skills"],
        hooks=hooks,
        subagents=[
            {
                "id": sub.id,
                "plugin": owner,
                "description": sub.description,
                "plugins": list(sub.plugins),
                "tools": list(sub.tools) if sub.tools is not None else None,
                "flow": sub.flow,
                "max_iters": sub.max_iters,
                "prefers": dict(sub.prefers),
                "structured_outputs": dict(sub.structured_outputs),
                "prompt": sub.prompt,
            }
            for owner, sub in collected["subagents"]
        ],
        flows=flows,
        catalogs=collected["catalogs"],
        commands=collected["commands"],
        prompt={},
        model=_model_union(requires, prefers, entries, agent["id"]),
        workflow_state=collected["workflow_state"],
        delta=delta.to_dict(),
        diagnostics=[],
    )
    generators = {
        key: (lambda generator=generator: generator(resolution))
        for key, generator in builder.catalog.generators.items()
    }
    composed: ComposedPrompt = compose(
        identity={"id": agent["id"], "name": agent["name"], "role": agent.get("role", "")},
        identity_text=identity_text,
        fragments=fragments,
        tools=tool_names,
        plugins=plugin_ids,
        generators=generators,
    )
    for warning in composed.warnings:
        builder.diagnostics.append(
            Diagnostic("warning", "fragment_budget", warning, warning.split(".", 1)[0])
        )
    resolution.prompt = composed.to_dict()
    errors = [row for row in builder.diagnostics if row.severity == "error"]
    if errors:
        raise ResolutionError(errors)
    resolution.diagnostics = [row.to_dict() for row in builder.diagnostics]
    resolution.digest = _digest(resolution.body())
    return resolution


def resolve_agent(
    catalog: Catalog,
    spec: AgentSpec,
    *,
    marketplace: str,
    delta: Delta = NO_DELTA,
    identity: Mapping[str, str] | None = None,
) -> Resolution:
    """Resolve ``spec`` (from ``marketplace``) with ``delta`` applied."""

    builder = _Builder(catalog, marketplace)
    selections = []
    depth_of = {layer: index for index, layer in enumerate(spec.layers)}
    for selection in spec.plugins:
        selections.append(
            (selection.ref, "agent", selection.layer, depth_of.get(selection.layer, 0))
        )
    removed = _resolve_set(builder, selections, delta, user_depth=len(spec.layers))
    entries = sorted(builder.entries.values(), key=lambda e: e.order)
    identity = dict(identity or {})
    agent = {
        "ref": f"{marketplace}/{spec.id}",
        "id": identity.get("id") or spec.id,
        "name": identity.get("name") or spec.name,
        "role": spec.role,
        "description": identity.get("description") or spec.description,
        "version": spec.version,
        "marketplace": marketplace,
        "flow": dict(spec.flow),
        "spec_checksum": spec.checksum,
        "parent": identity.get("parent", ""),
        "kind": "agent",
    }
    return _finish(
        builder,
        agent=agent,
        identity_text="",
        entries=entries,
        removed=removed,
        requires=spec.requires,
        prefers=spec.prefers,
        delta=delta,
        flow_id=str(spec.flow.get("id") or "react"),
    )


def resolve_subagent(catalog: Catalog, parent: Resolution, subagent_id: str) -> Resolution:
    """Resolve a defined subagent of ``parent`` with its own plugin set."""

    row = next((sub for sub in parent.subagents if sub["id"] == subagent_id), None)
    if row is None:
        raise ResolutionError(
            [
                Diagnostic(
                    "error",
                    "subagent_not_found",
                    f"{subagent_id!r} is not a subagent of this agent",
                    subagent_id,
                )
            ]
        )
    builder = _Builder(catalog, str(parent.agent.get("marketplace") or ""))
    selections = [
        (PluginRef.parse(CORE_PLUGIN), "core", "core", 0),
        (PluginRef.parse(row["plugin"]), "agent", row["plugin"], 1),
    ]
    selections += [(PluginRef.parse(ref), "agent", row["plugin"], 1) for ref in row["plugins"]]
    removed = _resolve_set(builder, selections, Delta(), user_depth=2)
    entries = sorted(builder.entries.values(), key=lambda e: e.order)
    agent = {
        "ref": f"{parent.agent.get('ref')}#{subagent_id}",
        "id": subagent_id,
        "name": subagent_id,
        "role": "",
        "description": row["description"],
        "version": parent.agent.get("version", ""),
        "marketplace": parent.agent.get("marketplace", ""),
        "flow": {"id": row.get("flow") or "react", "options": {}},
        "parent": parent.agent.get("ref", ""),
        "owner_plugin": row["plugin"],
        "kind": "subagent",
        "max_iters": row.get("max_iters", 0),
        "structured_outputs": row.get("structured_outputs") or {},
    }
    return _finish(
        builder,
        agent=agent,
        identity_text=row["prompt"],
        entries=entries,
        removed=removed,
        requires={},
        prefers=row.get("prefers") or {},
        delta=Delta(),
        tool_allowlist=row.get("tools"),
        flow_id=str(row.get("flow") or "react"),
    )
