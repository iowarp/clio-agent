"""The internal plugin record every source format is loaded into.

Native Agent Plugins packages and imported Claude Code plugins both become a
:class:`PluginRecord`. The record is plain data (JSON round-trippable): it is
what install stores, what resolution reads and what the UI lists. Executable
parts (Python tools, hooks, flows, subagent modules) are referenced by module
path and attribute and imported only for trusted plugins.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning"]
FRAGMENT_SECTIONS = ("guidelines", "rendering", "capabilities", "domain")


@dataclass(frozen=True)
class Diagnostic:
    """A typed load, import or resolution finding."""

    severity: Severity
    code: str
    message: str
    plugin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        where = f"{self.plugin}: " if self.plugin else ""
        return f"{where}{self.message} [{self.code}]"


@dataclass(frozen=True)
class SkillRecord:
    id: str
    description: str
    path: str


@dataclass(frozen=True)
class ToolRecord:
    """A tool the plugin contributes: a CLIO built-in it claims, or a Python tool."""

    name: str
    kind: Literal["native", "python"]
    module: str = ""
    attr: str = ""
    description: str = ""
    read_only: bool = False


@dataclass(frozen=True)
class HookRecord:
    """A hook: an SDK ``@hook`` function, or a command from an imported hooks file."""

    id: str
    event: str
    kind: Literal["python", "command"]
    module: str = ""
    attr: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    tools: str = ""
    timeout_ms: int = 30000
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    fail_closed: bool = False
    #: ``claude-code`` for a command imported from a Claude Code plugin: it reads and
    #: answers on that wire (``gact/hooks/claude_code_hooks.py``).
    dialect: str = ""


@dataclass(frozen=True)
class SubagentRecord:
    id: str
    description: str
    prompt: str
    plugins: tuple[str, ...]
    tools: tuple[str, ...] | None = None
    prefers: dict[str, Any] = field(default_factory=dict)
    flow: str = "react"
    max_iters: int = 0
    structured_outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlowRecord:
    id: str
    module: str
    attr: str
    description: str = ""


@dataclass(frozen=True)
class FragmentRecord:
    id: str
    section: str
    body: str
    path: str
    when_tools: tuple[str, ...] = ()
    when_plugins: tuple[str, ...] = ()
    generate: str = ""

    @property
    def tokens(self) -> int:
        """A deterministic size estimate: about four characters per token."""

        return (len(self.body) + 3) // 4


@dataclass(frozen=True)
class CommandRecord:
    id: str
    description: str
    path: str


@dataclass(frozen=True)
class CatalogRecord:
    id: str
    path: str


@dataclass(frozen=True)
class PluginRecord:
    """Everything a plugin contributes, as data."""

    id: str
    version: str
    description: str
    kind: Literal["plugin", "pack"]
    format: Literal["agent-plugins", "claude-code"]
    root: str
    marketplace: str = ""
    requires: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    model: dict[str, Any] = field(default_factory=dict)
    prefers: dict[str, Any] = field(default_factory=dict)
    skills: tuple[SkillRecord, ...] = ()
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: tuple[ToolRecord, ...] = ()
    hooks: tuple[HookRecord, ...] = ()
    subagents: tuple[SubagentRecord, ...] = ()
    flows: tuple[FlowRecord, ...] = ()
    fragments: tuple[FragmentRecord, ...] = ()
    commands: tuple[CommandRecord, ...] = ()
    catalogs: tuple[CatalogRecord, ...] = ()
    workflow_state: dict[str, Any] | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    executable_digest: str = ""
    text_digest: str = ""
    keywords: tuple[str, ...] = ()

    @property
    def errors(self) -> list[Diagnostic]:
        return [row for row in self.diagnostics if row.severity == "error"]

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def has_executable_parts(self) -> bool:
        # Native packages declare tools, subagents and flows in imported Python.
        python = self.format == "agent-plugins" and bool(self.tools or self.subagents)
        return bool(self.mcp_servers or self.hooks or self.flows or python)

    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginRecord":
        def rows(key: str, kind: type) -> tuple[Any, ...]:
            out = []
            for row in data.get(key) or []:
                values = dict(row)
                for name in ("args", "before", "after", "plugins", "when_tools", "when_plugins"):
                    if name in values and isinstance(values[name], list):
                        values[name] = tuple(values[name])
                if kind is SubagentRecord and isinstance(values.get("tools"), list):
                    values["tools"] = tuple(values["tools"])
                out.append(kind(**values))
            return tuple(out)

        return cls(
            id=data["id"],
            version=data.get("version") or "",
            description=data.get("description") or "",
            kind=data.get("kind") or "plugin",
            format=data.get("format") or "agent-plugins",
            root=data.get("root") or "",
            marketplace=data.get("marketplace") or "",
            requires=tuple(data.get("requires") or ()),
            members=tuple(data.get("members") or ()),
            model=dict(data.get("model") or {}),
            prefers=dict(data.get("prefers") or {}),
            skills=rows("skills", SkillRecord),
            mcp_servers={str(k): dict(v) for k, v in (data.get("mcp_servers") or {}).items()},
            tools=rows("tools", ToolRecord),
            hooks=rows("hooks", HookRecord),
            subagents=rows("subagents", SubagentRecord),
            flows=rows("flows", FlowRecord),
            fragments=rows("fragments", FragmentRecord),
            commands=rows("commands", CommandRecord),
            catalogs=rows("catalogs", CatalogRecord),
            workflow_state=data.get("workflow_state"),
            diagnostics=rows("diagnostics", Diagnostic),
            executable_digest=data.get("executable_digest") or "",
            text_digest=data.get("text_digest") or "",
            keywords=tuple(data.get("keywords") or ()),
        )
