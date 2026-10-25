"""Load a native Agent Plugins package into a :class:`PluginRecord`.

Reads ``plugin.json`` against Agent Plugins 1.0.0 plus the ``ai.iowarp.clio``
extension, then the folder conventions::

    plugin.json, skills/, mcp.json
    ai.iowarp.clio/plugin.py, tools/, hooks/, subagents/, commands/, flows/, prompt/, a2ui/

Python in ``ai.iowarp.clio/`` is imported only when the plugin is trusted. An
untrusted plugin still loads its text parts and digests, so the trust step can
show what it would run.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
import traceback
import types
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from clio_agent.plugins.frontmatter import FrontmatterError, split_frontmatter
from clio_agent.plugins.library import is_library_manifest, library_skills
from clio_agent.plugins.manifest import (
    NAMESPACE,
    ManifestError,
    clio_extension,
    read_manifest,
    read_mcp_config,
    within,
)
from clio_agent.plugins.package_files import package_files
from clio_agent.plugins.records import (
    FRAGMENT_SECTIONS,
    CatalogRecord,
    CommandRecord,
    Diagnostic,
    FlowRecord,
    FragmentRecord,
    HookRecord,
    PluginRecord,
    SkillRecord,
    SubagentRecord,
    ToolRecord,
)
from clio_agent.sdk.hooks import HOOK_EVENTS, HookSpec
from clio_agent.sdk.plugin import declaration_of
from clio_agent.sdk.subagent import Subagent
from clio_agent.sdk.tools import NativeToolRef, ToolSpec
from clio_agent.sdk.versions import PluginRef

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$")
_PYTHON_DIRS = ("tools", "hooks", "subagents", "flows")
#: Files whose change needs the trust step again (design: executable parts).
_EXECUTABLE_GLOBS = ("mcp.json", ".mcp.json", "hooks/hooks.json", f"{NAMESPACE}/**/*.py")
#: Files that only describe or instruct: a change to them applies without the trust
#: step. Every other file counts as executable, since an MCP server or hook command
#: can run any file in the package through ``${PLUGIN_ROOT}``.
_TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
)
_TEXT_FILES = frozenset({"plugin.json", ".claude-plugin/plugin.json", "LICENSE", "LICENSE.txt"})


class PluginLoadError(ValueError):
    """The package cannot be loaded at all (fatal manifest error)."""


class PluginImportError(PluginLoadError):
    """A plugin or marketplace Python module raised while importing."""


def import_module_typed(name: str, label: str) -> types.ModuleType:
    """Import ``name``; any failure of the imported (untrusted) code is a typed error."""

    try:
        return importlib.import_module(name)
    except Exception as exc:
        detail = traceback.format_exception_only(type(exc), exc)[-1].strip()
        raise PluginImportError(f"{label}: {detail}") from exc


_ROOT_TOKENS = ("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}")


def referenced_files(root: Path, servers: Mapping[str, Mapping[str, Any]]) -> set[Path]:
    """Files under ``root`` that an MCP server's command, args, env or cwd names.

    Such a file runs whatever its name, so it is executable for the digest; a named
    folder makes every file under it executable.
    """

    texts: list[str] = []
    for entry in servers.values():
        texts += [str(entry.get("command") or ""), str(entry.get("cwd") or "")]
        texts += [str(arg) for arg in entry.get("args") or ()]
        texts += [str(value) for value in (entry.get("env") or {}).values()]
    found: set[Path] = set()
    for text in texts:
        for token in _ROOT_TOKENS:
            text = text.replace(token, str(root))
        for piece in re.split(r"[\s=:,;]+", text):
            if not piece:
                continue
            candidate = Path(piece) if Path(piece).is_absolute() else root / piece
            if not within(root, candidate):
                continue
            if candidate.is_file():
                found.add(candidate.resolve())
            elif candidate.is_dir() and candidate.resolve() != root.resolve():
                found.update(p.resolve() for p in candidate.rglob("*") if p.is_file())
    return found


def tree_digests(
    root: Path,
    executable_globs: Iterable[str] = _EXECUTABLE_GLOBS,
    referenced: Iterable[Path] = (),
) -> tuple[str, str]:
    """Return ``(executable_digest, text_digest)`` over the package files.

    A file is text when its suffix or name says it only describes (``_TEXT_SUFFIXES``,
    ``_TEXT_FILES``) and neither an ``executable_globs`` pattern nor ``referenced``
    (:func:`referenced_files`, a hook's argv) names it; everything else, including a
    server binary or a helper script, is executable.
    """

    named: set[Path] = {Path(path).resolve() for path in referenced}
    for pattern in executable_globs:
        named.update(path.resolve() for path in root.glob(pattern) if path.is_file())
    exec_hash = hashlib.sha256()
    text_hash = hashlib.sha256()
    for path in sorted(package_files(root)):
        relative = path.relative_to(root).as_posix()
        text = path.suffix.lower() in _TEXT_SUFFIXES or relative in _TEXT_FILES
        target = exec_hash if path.resolve() in named or not text else text_hash
        target.update(relative.encode("utf-8") + b"\0")
        target.update(hashlib.sha256(path.read_bytes()).digest())
    return exec_hash.hexdigest(), text_hash.hexdigest()


def _package_name(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"_clio_plugin_{digest}"


def import_plugin_module(root: Path, relative: str) -> types.ModuleType:
    """Import ``ai.iowarp.clio/<relative>`` of the plugin at ``root``.

    The plugin's ``ai.iowarp.clio`` folder becomes a private package, so plugin
    files may import each other relatively. Imports are cached per resolved root,
    and an installed plugin's root changes with its version.
    """

    clio_dir = root / NAMESPACE
    package = _package_name(root)
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(clio_dir)]
        module.__package__ = package
        sys.modules[package] = module
    parts = Path(relative).with_suffix("").parts
    for depth in range(1, len(parts)):
        name = ".".join((package, *parts[:depth]))
        if name not in sys.modules:
            sub = types.ModuleType(name)
            sub.__path__ = [str(clio_dir.joinpath(*parts[:depth]))]
            sub.__package__ = name
            sys.modules[name] = sub
    return import_module_typed(".".join((package, *parts)), relative)


def load_attribute(root: str | Path, module: str, attr: str) -> Any:
    """Return ``attr`` of a plugin module recorded at load time."""

    return getattr(import_plugin_module(Path(root), module), attr)


def _python_files(clio_dir: Path, folder: str) -> list[Path]:
    base = clio_dir / folder
    if not base.is_dir():
        return []
    return sorted(path for path in base.glob("*.py") if path.name != "__init__.py")


def _skills(
    root: Path, plugin: str, only: Sequence[str] | None = None
) -> tuple[list[SkillRecord], list[Diagnostic]]:
    """The plugin's skills: every folder under ``skills/``, or exactly the folders
    ``only`` names (a Claude Code marketplace entry's ``skills`` list)."""

    out: list[SkillRecord] = []
    diagnostics: list[Diagnostic] = []
    if only is not None:
        children = []
        for relative in only:
            child = (root / relative).resolve()
            if not within(root, child) or not (child / "SKILL.md").is_file():
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "skill_missing",
                        f"listed skill {relative!r} is not a skill folder",
                        plugin,
                    )
                )
                continue
            children.append(child)
    else:
        skills_dir = root / "skills"
        if not skills_dir.exists():
            return [], []
        if not skills_dir.is_dir() or not within(root, skills_dir):
            return [], [Diagnostic("error", "skills_invalid", "skills is not a directory", plugin)]
        children = sorted(skills_dir.iterdir())
    for child in children:
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not skill_file.is_file():
            continue
        if not within(root, skill_file):
            diagnostics.append(
                Diagnostic("warning", "skill_outside_root", f"skill {child.name} escapes", plugin)
            )
            continue
        try:
            meta, _ = split_frontmatter(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            diagnostics.append(
                Diagnostic("warning", "skill_invalid", f"skill {child.name}: {exc}", plugin)
            )
            continue
        meta = meta or {}
        name = str(meta.get("name") or "")
        description = str(meta.get("description") or "").strip()
        problem = ""
        if not name:
            problem = "SKILL.md has no name"
        elif name != child.name and child.resolve() != root.resolve():
            # A skill installed on its own is the plugin's root, held in a folder
            # named by its content digest: only skills under skills/ match a folder.
            problem = f"name {name!r} does not match its directory {child.name!r}"
        elif len(name) > 64 or not _SKILL_NAME_RE.match(name):
            problem = f"name {name!r} must be lowercase letters, digits and single hyphens"
        elif not description:
            problem = "SKILL.md has no description"
        if problem:
            diagnostics.append(
                Diagnostic("warning", "skill_invalid", f"skill {child.name}: {problem}", plugin)
            )
            continue
        if len(description) > 1024:
            # Over the Agent Skills limit, but the skill itself is fine: keep it, say so.
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "skill_description_long",
                    f"skill {child.name}: description is {len(description)} characters, "
                    "over the 1024 the Agent Skills format allows",
                    plugin,
                )
            )
        out.append(SkillRecord(id=name, description=description, path=str(skill_file)))
    return out, diagnostics


def _fragments_and_texts(
    clio_dir: Path, plugin: str
) -> tuple[list[FragmentRecord], dict[str, str], list[Diagnostic]]:
    prompt_dir = clio_dir / "prompt"
    fragments: list[FragmentRecord] = []
    texts: dict[str, str] = {}
    diagnostics: list[Diagnostic] = []
    if not prompt_dir.is_dir():
        return fragments, texts, diagnostics
    for path in sorted(prompt_dir.rglob("*.md")):
        relative = path.relative_to(clio_dir).as_posix()
        try:
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError) as exc:
            diagnostics.append(
                Diagnostic("error", "fragment_invalid", f"{relative}: {exc}", plugin)
            )
            continue
        if meta is None or "section" not in meta:
            texts[relative] = body.strip()
            continue
        fragment_id = str(meta.get("id") or "")
        section = str(meta.get("section") or "")
        when = meta.get("when") or {}
        problem = ""
        if not fragment_id.startswith(f"{plugin}."):
            problem = f"fragment id {fragment_id!r} must start with '{plugin}.'"
        elif section not in FRAGMENT_SECTIONS:
            problem = f"section {section!r} must be one of {', '.join(FRAGMENT_SECTIONS)}"
        elif section in ("guidelines", "rendering") and plugin != "core":
            problem = f"only the core plugin contributes the {section} section"
        elif not isinstance(when, dict) or set(when) - {"tools", "plugins"}:
            problem = "when may only hold tools and plugins lists"
        if problem:
            diagnostics.append(
                Diagnostic("error", "fragment_invalid", f"{relative}: {problem}", plugin)
            )
            continue
        fragments.append(
            FragmentRecord(
                id=fragment_id,
                section=section,
                body=body.strip(),
                path=str(path),
                when_tools=tuple(str(t) for t in when.get("tools") or ()),
                when_plugins=tuple(str(p) for p in when.get("plugins") or ()),
                generate=str(meta.get("generate") or ""),
            )
        )
    return fragments, texts, diagnostics


def _commands(clio_dir: Path, plugin: str) -> list[CommandRecord]:
    base = clio_dir / "commands"
    out: list[CommandRecord] = []
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.md")):
        try:
            meta, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, FrontmatterError):
            meta = None
        meta = meta or {}
        name = str(meta.get("slash_id") or meta.get("slash-id") or meta.get("name") or "")
        out.append(
            CommandRecord(
                # The command's id, as command discovery names it (its ``name``).
                id=name.strip().lstrip("/") or path.stem,
                description=str((meta or {}).get("description") or ""),
                path=str(path),
            )
        )
    return out


def _catalogs(clio_dir: Path) -> list[CatalogRecord]:
    base = clio_dir / "a2ui"
    if not base.is_dir():
        return []
    return [
        CatalogRecord(id=child.name, path=str(child))
        for child in sorted(base.iterdir())
        if child.is_dir() and (child / "catalog.json").is_file()
    ]


def _qualify(plugin: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name if "." in name else f"{plugin}.{name}" for name in names)


class _PythonParts:
    def __init__(self) -> None:
        self.declaration: dict[str, Any] | None = None
        self.tools: list[ToolRecord] = []
        self.hooks: list[HookRecord] = []
        self.subagents: list[SubagentRecord] = []
        self.flows: list[FlowRecord] = []
        self.diagnostics: list[Diagnostic] = []


def _import(root: Path, relative: str, plugin: str, parts: _PythonParts) -> types.ModuleType | None:
    try:
        return import_plugin_module(root, relative)
    except PluginImportError as exc:
        parts.diagnostics.append(Diagnostic("error", "plugin_import_failed", str(exc), plugin))
        return None


def _defined_here(module: types.ModuleType) -> list[tuple[str, Any]]:
    return [(name, value) for name, value in vars(module).items() if not name.startswith("_")]


def _python_parts(root: Path, plugin: str, texts: dict[str, str]) -> _PythonParts:
    # A Flow is a dspy.Module: loading a trusted plugin's Python parts loads the engine.
    from clio_agent.sdk.flow import Flow  # noqa: PLC0415

    parts = _PythonParts()
    clio_dir = root / NAMESPACE
    if (clio_dir / "plugin.py").is_file():
        module = _import(root, "plugin.py", plugin, parts)
        if module is not None:
            found = [
                declaration_of(value)
                for _, value in _defined_here(module)
                if getattr(value, "__module__", module.__name__) == module.__name__
                or not isinstance(value, type)
            ]
            declarations = [d for d in found if d is not None]
            if len(declarations) != 1:
                parts.diagnostics.append(
                    Diagnostic(
                        "error",
                        "plugin_declaration_invalid",
                        f"plugin.py must declare exactly one Plugin or Pack, found {len(declarations)}",
                        plugin,
                    )
                )
            else:
                parts.declaration = declarations[0].describe()
    for folder in _PYTHON_DIRS:
        for path in _python_files(clio_dir, folder):
            relative = path.relative_to(clio_dir).as_posix()
            module = _import(root, relative, plugin, parts)
            if module is None:
                continue
            for attr, value in _defined_here(module):
                if folder == "tools" and isinstance(value, ToolSpec):
                    parts.tools.append(
                        ToolRecord(
                            name=value.name,
                            kind="python",
                            module=relative,
                            attr=attr,
                            description=value.description,
                            read_only=value.read_only,
                        )
                    )
                elif folder == "tools" and isinstance(value, NativeToolRef):
                    parts.tools.append(ToolRecord(name=value.name, kind="native"))
                elif folder == "hooks" and isinstance(value, HookSpec):
                    parts.hooks.append(
                        HookRecord(
                            id=f"{plugin}.{value.id}",
                            event=HOOK_EVENTS[value.event],
                            kind="python",
                            module=relative,
                            attr=attr,
                            tools=value.tools,
                            before=_qualify(plugin, value.before),
                            after=_qualify(plugin, value.after),
                            fail_closed=value.fail_closed,
                        )
                    )
                elif folder == "subagents" and isinstance(value, Subagent):
                    record = _subagent_record(value, texts, root, plugin, parts)
                    if record is not None:
                        parts.subagents.append(record)
                elif (
                    folder == "flows"
                    and isinstance(value, type)
                    and issubclass(value, Flow)
                    and value is not Flow
                    and value.__module__ == module.__name__
                ):
                    if not value.id:
                        parts.diagnostics.append(
                            Diagnostic(
                                "error", "flow_invalid", f"{relative}:{attr} has no id", plugin
                            )
                        )
                        continue
                    parts.flows.append(
                        FlowRecord(
                            id=value.id,
                            module=relative,
                            attr=attr,
                            description=value.description or (value.__doc__ or "").strip(),
                            shape=str(getattr(value, "shape", "") or ""),
                        )
                    )
    return parts


def _subagent_record(
    value: Subagent, texts: dict[str, str], root: Path, plugin: str, parts: _PythonParts
) -> SubagentRecord | None:
    described = value.describe()
    prompt = described["prompt"]
    if "file" in prompt:
        relative = str(prompt["file"])
        path = root / NAMESPACE / relative
        if relative in texts:
            text = texts[relative]
        elif path.is_file() and within(root, path):
            text = path.read_text(encoding="utf-8").strip()
        else:
            parts.diagnostics.append(
                Diagnostic(
                    "error",
                    "prompt_file_missing",
                    f"subagent {value.id!r}: prompt file {NAMESPACE}/{relative} does not exist",
                    plugin,
                )
            )
            return None
    else:
        text = str(prompt.get("text") or "")
    return SubagentRecord(
        id=described["id"],
        description=described["description"],
        prompt=text,
        plugins=tuple(described["plugins"]),
        tools=tuple(described["tools"]) if described["tools"] is not None else None,
        prefers=described["prefers"],
        flow=described["flow"],
        max_iters=described["max_iters"],
        structured_outputs=described["structured_outputs"],
        internal=described["internal"],
    )


def _merge_refs(
    plugin: str, field: str, *sources: Iterable[str]
) -> tuple[tuple[str, ...], list[Diagnostic]]:
    lists = [tuple(str(PluginRef.parse(v)) for v in source) for source in sources if source]
    if len({tuple(sorted(values)) for values in lists}) > 1:
        return lists[0], [
            Diagnostic(
                "error",
                "plugin_declaration_conflict",
                f"plugin.py and plugin.json declare different {field}",
                plugin,
            )
        ]
    return (lists[0] if lists else ()), []


def load_plugin(root: Path, *, marketplace: str = "", trusted: bool) -> PluginRecord:
    """Load the native plugin at ``root``. Raises :class:`PluginLoadError` if fatal."""

    root = root.resolve()
    try:
        manifest, diagnostics = read_manifest(root)
        plugin = manifest["name"]
        raw = dict((manifest.get("extensions") or {}).get(NAMESPACE) or {})
        library = raw if is_library_manifest(raw) else None
        ext = {} if library is not None else clio_extension(manifest, plugin)
    except ManifestError as exc:
        raise PluginLoadError(str(exc)) from exc
    servers, server_diags = read_mcp_config(root, plugin)
    exec_digest, text_digest = tree_digests(root, referenced=referenced_files(root, servers))
    skills, skill_diags = _skills(root, plugin)
    if library is not None:
        mapped, library_diags = library_skills(root, plugin, library)
        known = {skill.id for skill in skills}
        skills += [skill for skill in mapped if skill.id not in known]
        skill_diags += library_diags
    clio_dir = root / NAMESPACE
    fragments, texts, fragment_diags = _fragments_and_texts(clio_dir, plugin)
    diagnostics += skill_diags + server_diags + fragment_diags
    has_python = (
        any((clio_dir / name).is_dir() for name in _PYTHON_DIRS)
        or (clio_dir / "plugin.py").is_file()
    )
    parts = _PythonParts()
    if trusted:
        parts = _python_parts(root, plugin, texts)
        diagnostics += parts.diagnostics
    elif has_python:
        diagnostics.append(
            Diagnostic(
                "warning",
                "plugin_untrusted",
                "Python parts were not loaded because the plugin is not trusted",
                plugin,
            )
        )
    declaration = parts.declaration or {}
    if declaration and declaration.get("id") != plugin:
        diagnostics.append(
            Diagnostic(
                "error",
                "plugin_declaration_conflict",
                f"plugin.py declares id {declaration.get('id')!r} but plugin.json names {plugin!r}",
                plugin,
            )
        )
    kind: Literal["plugin", "pack"] = (
        "pack" if (declaration.get("kind") or ext.get("kind")) == "pack" else "plugin"
    )
    if declaration and ext.get("kind") and declaration.get("kind") != ext.get("kind"):
        diagnostics.append(
            Diagnostic(
                "error",
                "plugin_declaration_conflict",
                "plugin.py and plugin.json disagree on kind",
                plugin,
            )
        )
    requires, conflict = _merge_refs(
        plugin, "requires", declaration.get("requires") or (), ext.get("requires") or ()
    )
    diagnostics += conflict
    members, conflict = _merge_refs(
        plugin, "members", declaration.get("members") or (), ext.get("members") or ()
    )
    diagnostics += conflict
    version = str(manifest.get("version") or declaration.get("version") or "0.0.0")
    if declaration.get("version") and declaration["version"] != version:
        diagnostics.append(
            Diagnostic(
                "error",
                "plugin_declaration_conflict",
                f"plugin.py version {declaration['version']!r} differs from plugin.json {version!r}",
                plugin,
            )
        )
    workflow_state = ext.get("workflow_state")
    if workflow_state is not None:
        from clio_agent.gact.workflow_state.schema import WorkflowStateSchema  # noqa: PLC0415

        try:
            WorkflowStateSchema.model_validate(workflow_state)
        except ValueError as exc:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "workflow_state_invalid",
                    f"workflow_state does not match the workflow_state schema: {exc}",
                    plugin,
                )
            )
    record = PluginRecord(
        id=plugin,
        version=version,
        description=str(manifest.get("description") or ""),
        kind=kind,
        format="agent-plugins",
        root=str(root),
        marketplace=marketplace,
        requires=requires,
        members=members,
        model=dict(declaration.get("model") or ext.get("model") or {}),
        prefers=dict(declaration.get("prefers") or ext.get("prefers") or {}),
        skills=tuple(skills),
        mcp_servers=servers,
        tools=tuple(parts.tools),
        hooks=tuple(parts.hooks),
        subagents=tuple(parts.subagents),
        flows=tuple(parts.flows),
        fragments=tuple(fragments),
        commands=tuple(_commands(clio_dir, plugin)),
        catalogs=tuple(
            [CatalogRecord(id=str(c), path="") for c in ext.get("catalogs") or ()]
            + _catalogs(clio_dir)
        ),
        workflow_state=workflow_state,
        diagnostics=tuple(diagnostics),
        executable_digest=exec_digest,
        text_digest=text_digest,
        keywords=tuple(manifest.get("keywords") or ()),
        python_pending=has_python and not trusted,
    )
    if kind == "pack":
        content = [
            name
            for name, present in (
                ("skills", record.skills),
                ("MCP servers", record.mcp_servers),
                ("tools", record.tools),
                ("hooks", record.hooks),
                ("subagents", record.subagents),
                ("flows", record.flows),
            )
            if present
        ]
        if content or not members:
            message = (
                f"a pack holds only membership, but this one has {', '.join(content)}"
                if content
                else "a pack must list its members"
            )
            record = _with(record, Diagnostic("error", "pack_invalid", message, plugin))
    return record


def _with(record: PluginRecord, diagnostic: Diagnostic) -> PluginRecord:
    from dataclasses import replace  # noqa: PLC0415

    return replace(record, diagnostics=(*record.diagnostics, diagnostic))
