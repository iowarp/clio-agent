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

import ast
import contextlib
import contextvars
import copy
import dataclasses
import hashlib
import importlib
import importlib.util
import os
import re
import sys
import traceback
import types
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any, Literal

from clio_agent.plugins.commands import read_commands
from clio_agent.plugins.frontmatter import FrontmatterError, split_frontmatter
from clio_agent.plugins.library import is_library_manifest, library_skills
from clio_agent.plugins.manifest import (
    NAMESPACE,
    ManifestError,
    clio_extension,
    host_semantics,
    read_manifest,
    read_mcp_config,
    within,
)
from clio_agent.plugins.package_files import package_files
from clio_agent.plugins.records import (
    FRAGMENT_SECTIONS,
    FRAGMENT_SESSIONS,
    FRAGMENT_WHEN_KEYS,
    CatalogRecord,
    Diagnostic,
    FlowRecord,
    FragmentRecord,
    HookRecord,
    PluginRecord,
    SkillRecord,
    SubagentRecord,
    ToolRecord,
)
from clio_agent.plugins.workflows import read_workflows
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
#: ``scheme://`` or a pip/uv VCS requirement (``git+…``): a URL, never split on ``:``.
_URL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*://|git\+)")


def _path_pieces(text: str) -> Iterator[str]:
    """The pieces of a server's command, arg, env or cwd text that may name a path.

    Words split on whitespace and ``=,;``; a word that is not a URL also splits on
    ``:`` (``PATH``-style lists). A URL names no file, except a local ``file:`` URL.
    """

    for word in re.split(r"[\s=,;]+", text):
        if not word:
            continue
        if _URL.match(word):
            if word[:5].lower() == "file:":
                from urllib.parse import urlsplit  # noqa: PLC0415
                from urllib.request import url2pathname  # noqa: PLC0415

                parts = urlsplit(word)
                if parts.netloc in ("", "localhost") and parts.path:
                    yield url2pathname(parts.path)
            continue
        yield from (piece for piece in word.split(":") if piece)


def _drive(path: PurePath) -> str:
    return path.drive.replace("/", "\\").casefold()


def _network_share(path: PurePath) -> bool:
    """Whether ``path`` is on a network share (a UNC drive; only Windows paths have one)."""

    drive = _drive(path)
    if drive.startswith(("\\\\?\\", "\\\\.\\")):
        return drive[4:].startswith("unc\\")
    return drive.startswith("\\\\")


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
        for piece in _path_pieces(text):
            candidate = Path(piece) if Path(piece).is_absolute() else root / piece
            # Resolving another network share asks that host, which can block for many
            # seconds, and no share names a file inside a local plugin root.
            if _network_share(candidate) and _drive(candidate) != _drive(root):
                continue
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
        target.update(file_digest(path))
    return exec_hash.hexdigest(), text_hash.hexdigest()


#: Within :func:`hashing_once`, each file's ``(size, mtime_ns, sha256)`` by absolute path.
_FILE_HASHES: contextvars.ContextVar[dict[str, tuple[int, int, bytes]] | None] = (
    contextvars.ContextVar("clio_plugin_file_hashes", default=None)
)


@contextlib.contextmanager
def hashing_once() -> Iterator[None]:
    """Read and hash each unchanged package file once, for the length of an install.

    An install digests the same files several times (the marketplace, its snapshot's
    name, each plugin untrusted and then trusted). Every read is a file open, which
    real-time antivirus scans; on Windows these were most of the default marketplace's
    install. A file is re-read when its size or modification time changed.
    """

    token = _FILE_HASHES.set({}) if _FILE_HASHES.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _FILE_HASHES.reset(token)


def file_digest(path: Path) -> bytes:
    """The SHA-256 of ``path``'s bytes, read once per unchanged file in :func:`hashing_once`."""

    memo = _FILE_HASHES.get()
    if memo is None:
        return hashlib.sha256(path.read_bytes()).digest()
    key = os.path.abspath(path)
    stat = os.stat(key)
    known = memo.get(key)
    if known is not None and known[:2] == (stat.st_size, stat.st_mtime_ns):
        return known[2]
    digest = hashlib.sha256(path.read_bytes()).digest()
    memo[key] = (stat.st_size, stat.st_mtime_ns, digest)
    return digest


def carry_digests(source: Path, copy: Path) -> None:
    """Give ``copy`` (a copy of ``source`` that keeps modification times) its digests.

    Only a file whose source is unchanged since it was hashed, and whose copy has its
    size and modification time, takes its source's digest without being read.
    """

    memo = _FILE_HASHES.get()
    if not memo:
        return
    prefix = os.path.join(os.path.abspath(source), "")
    target = os.path.abspath(copy)
    for key, (size, mtime, digest) in list(memo.items()):
        if not key.startswith(prefix):
            continue
        copied = os.path.join(target, key[len(prefix) :])
        try:
            now, there = os.stat(key), os.stat(copied)
        except OSError:
            continue
        if (now.st_size, now.st_mtime_ns) == (size, mtime) == (there.st_size, there.st_mtime_ns):
            memo[copied] = (size, mtime, digest)


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
        elif not isinstance(when, dict) or set(when) - FRAGMENT_WHEN_KEYS:
            problem = f"when may hold only {', '.join(sorted(FRAGMENT_WHEN_KEYS))}"
        elif str(when.get("session") or "") not in FRAGMENT_SESSIONS:
            problem = f"when.session must be one of {', '.join(sorted(FRAGMENT_SESSIONS - {''}))}"
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
                when_session=str(when.get("session") or ""),
                when_providers=tuple(str(p) for p in when.get("providers") or ()),
                when_models=tuple(str(m) for m in when.get("models") or ()),
            )
        )
    return fragments, texts, diagnostics


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


def _flow_base() -> type | None:
    """``Flow``, once some flow module has imported it; ``None`` while none has.

    A flow subclasses ``Flow``, so a module defining one has imported
    ``clio_agent.sdk.flow`` (and its engine) itself: a plugin without flows never loads it.
    """

    module = sys.modules.get("clio_agent.sdk.flow")
    return getattr(module, "Flow", None)


#: Where a flow module may take ``Flow`` from, for :func:`_static_flows`.
_FLOW_HOMES = frozenset({"clio_agent.sdk", "clio_agent.sdk.flow"})
#: Names whose use may bind module globals at import: their module is imported instead.
_DYNAMIC_NAMES = frozenset({"globals", "vars", "locals", "exec", "eval", "setattr", "__import__"})
#: Builtins an import-time call may not reach: each can make a class of the module.
_CLASS_MAKERS = frozenset({"type", "__build_class__"})
_FLOW_FIELDS = frozenset({"id", "description", "shape"})
#: What a flow class may not bind beyond its fields: each changes what the import reads.
_FLOW_CLASS_NAMES = frozenset({"__doc__", "__module__"})
#: What reaches a module's namespace or a class's fields from code: its module is imported.
_REACHING_ATTRS = frozenset(
    {
        "modules",
        "__dict__",
        "f_globals",
        "f_locals",
        "f_back",
        "__globals__",
        "import_module",
        "getmodule",
        "reload",
        "__setattr__",
        "__delattr__",
    }
)
_REACHING_NAMES = _DYNAMIC_NAMES | {"__loader__", "__spec__", "__builtins__", "delattr"}
_UNKNOWN = object()


def _import_time(node: ast.AST, *, classes: bool = True) -> Iterator[ast.AST]:
    """``node`` and what in it runs when its module is imported: no function or lambda body.

    With ``classes=False`` a nested class contributes only its name, decorators, bases
    and keywords, the parts that run in the enclosing namespace.
    """

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = current.args
            stack.extend(arguments.defaults)
            stack.extend(default for default in arguments.kw_defaults if default is not None)
            if not isinstance(current, ast.Lambda):
                stack.extend(current.decorator_list)
            continue
        if isinstance(current, ast.ClassDef) and current is not node and not classes:
            stack.extend(current.decorator_list)
            stack.extend(current.bases)
            stack.extend(keyword.value for keyword in current.keywords)
            continue
        stack.extend(ast.iter_child_nodes(current))


def _bound_names(nodes: Iterable[ast.AST]) -> Iterator[str]:
    """Every name ``nodes`` bind or delete in the namespace they run in."""

    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, ast.alias):
            yield node.asname or node.name.partition(".")[0]
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest


def _class_fields(node: ast.ClassDef) -> dict[str, str] | None:
    """The literal ``id``/``description``/``shape`` a flow class sets, ``None`` if unprovable."""

    fields: dict[str, str] = {}
    for item in node.body:
        simple: list[str] = []
        if isinstance(item, ast.Assign) and all(isinstance(t, ast.Name) for t in item.targets):
            simple = [t.id for t in item.targets if isinstance(t, ast.Name)]
            checked: Iterable[ast.AST] = _import_time(item.value, classes=False)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            simple = [item.target.id] if item.value is not None else []
            checked = _import_time(item.value, classes=False) if item.value is not None else ()
        else:
            checked = _import_time(item, classes=False)
        if any(name in _FLOW_FIELDS | _FLOW_CLASS_NAMES for name in _bound_names(checked)):
            return None
        for name in simple:
            if name in _FLOW_CLASS_NAMES:
                return None
            if name not in _FLOW_FIELDS:
                continue
            value = item.value
            try:
                literal = ast.literal_eval(value) if value is not None else None
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return None
            if not isinstance(literal, str):
                return None
            fields[name] = literal
    return fields


def _class_doc(node: ast.ClassDef) -> str | None:
    """``__doc__`` of the class ``node`` defines, as the compiler stores it."""

    first = node.body[0] if node.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return None
    # The compiler cleans a docstring's indentation; compiling only the docstring into an
    # empty class gives exactly what it stores, with none of the module's code run.
    probe = ast.Module(
        body=[ast.ClassDef(name="_Doc", bases=[], keywords=[], body=[first], decorator_list=[])],
        type_ignores=[],
    )
    namespace: dict[str, Any] = {}
    exec(compile(ast.fix_missing_locations(probe), "<flow-doc>", "exec"), namespace)  # noqa: S102
    return namespace["_Doc"].__doc__


def _call_root(func: ast.expr) -> ast.expr:
    while isinstance(func, (ast.Attribute, ast.Subscript, ast.Call)):
        func = func.func if isinstance(func, ast.Call) else func.value
    return func


def _static_flows(path: Path, relative: str, plugin: str) -> _PythonParts | None:
    """The flows module ``path`` declares, read from its source without importing it.

    Importing a flows module runs its imports (DSPy and the flow engine, through
    ``clio_agent.sdk.flow``) only to read each flow's ``id``, ``description`` and
    ``shape``; the class itself is imported when the flow runs (``flows.runner``).
    Only a module whose flows its source states plainly is read here:

    * flows are top-level classes, bound once, extending ``Flow`` (imported from
      ``clio_agent.sdk``) or one such class of the module, with no decorator or
      keyword, and literal fields;
    * no code reaches a module namespace (``global``, ``globals()``,
      ``sys.modules``, ``f_globals``...) or assigns a flow field as an attribute;
    * module-level code calls only builtins other than ``type`` and names imported
      from outside ``clio_agent``, and names neither ``Flow``, a flow class (except
      as a base) nor a function of the module.

    Anything else returns ``None`` and the module is imported as before, so the
    records are the import's own. Packages outside ``clio_agent`` are taken not to
    define classes into the module they are called from.
    """

    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            return None
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return None
        if isinstance(node, ast.Name) and node.id in _REACHING_NAMES:
            return None
        if isinstance(node, ast.Attribute) and (
            node.attr in _REACHING_ATTRS
            or (
                isinstance(node.ctx, (ast.Store, ast.Del))
                and node.attr in _FLOW_FIELDS | _FLOW_CLASS_NAMES
            )
        ):
            return None
    import builtins  # noqa: PLC0415

    # What each module-level name is bound to: "flow" (Flow itself), "other" (imported
    # from outside clio_agent), "function", a ClassDef of this module, or _UNKNOWN.
    bound: dict[str, Any] = {}
    counts: dict[str, int] = {}
    classes: list[ast.ClassDef] = []

    def bind(name: str, value: Any) -> None:
        counts[name] = counts.get(name, 0) + 1
        bound[name] = value if counts[name] == 1 else _UNKNOWN

    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                local = alias.asname or alias.name.partition(".")[0]
                external = alias.name.partition(".")[0] != "clio_agent"
                bind(local, "other" if external else _UNKNOWN)
        elif isinstance(stmt, ast.ImportFrom):
            source = stmt.module or ""
            external = not stmt.level and source.partition(".")[0] != "clio_agent"
            for alias in stmt.names:
                local = alias.asname or alias.name
                if not stmt.level and source in _FLOW_HOMES and alias.name == "Flow":
                    bind(local, "flow")
                else:
                    bind(local, "other" if external else _UNKNOWN)
        elif isinstance(stmt, ast.ClassDef):
            bind(stmt.name, stmt)
            classes.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bind(stmt.name, "function")
        else:
            nodes = list(_import_time(stmt, classes=False))
            if any(isinstance(node, ast.ClassDef) for node in nodes):
                return None  # a class bound under a condition: the import decides
            for name in _bound_names(nodes):
                bind(name, _UNKNOWN)

    kinds: dict[str, str] = {}  # class name -> "flow" | "other"

    def base_kind(base: ast.expr) -> str:
        if isinstance(base, ast.Subscript):
            base = base.value
        if isinstance(base, ast.Attribute):
            root = _call_root(base)
            return "other" if isinstance(root, ast.Name) and bound.get(root.id) == "other" else ""
        if not isinstance(base, ast.Name):
            return ""
        if base.id not in bound:
            return "other" if isinstance(getattr(builtins, base.id, None), type) else ""
        target = bound[base.id]
        if target in ("flow", "other"):
            return target
        if isinstance(target, ast.ClassDef):
            return kinds.get(target.name, "")
        return ""

    fields_of: dict[str, dict[str, str]] = {}
    found: list[tuple[ast.ClassDef, dict[str, str]]] = []
    for node in classes:
        if bound.get(node.name) is not node:
            return None  # rebound after its definition
        bases = [base_kind(base) for base in node.bases]
        if "" in bases:
            return None
        if "flow" not in bases:
            kinds[node.name] = "other"
            continue
        if node.decorator_list or node.keywords or len(node.bases) != 1:
            return None
        own = _class_fields(node)
        if own is None:
            return None
        parent = node.bases[0]
        inherited = (
            fields_of[bound[parent.id].name]
            if isinstance(parent, ast.Name) and isinstance(bound.get(parent.id), ast.ClassDef)
            else {}
        )
        kinds[node.name] = "flow"
        fields_of[node.name] = {**inherited, **own}
        if not node.name.startswith("_"):
            found.append((node, fields_of[node.name]))

    # Module-level code may bind a module global to anything it can reach, so it may not
    # reach a flow class, Flow or a function of the module (which could make one).
    hidden = {name for name, kind in kinds.items() if kind == "flow"}
    hidden |= {name for name, target in bound.items() if target in ("flow", "function")}

    def callable_at_import(func: ast.expr) -> bool:
        root = _call_root(func)
        if isinstance(root, ast.Constant):
            return True
        if not isinstance(root, ast.Name):
            return False
        if root.id not in bound:
            return root.id not in _CLASS_MAKERS and hasattr(builtins, root.id)
        return bound[root.id] == "other"

    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            bases = {id(base) for base in stmt.bases}
            nodes: list[ast.AST] = [*stmt.decorator_list, *stmt.bases]
            nodes += [keyword.value for keyword in stmt.keywords]
            nodes = [n for top in nodes for n in _import_time(top)]
            callees = [*stmt.decorator_list, *(keyword.value for keyword in stmt.keywords)]
        else:
            bases = set()
            nodes = list(_import_time(stmt, classes=False))
            callees = []
        for node in nodes:
            if isinstance(node, ast.Name) and node.id in hidden and id(node) not in bases:
                return None
            if isinstance(node, ast.Call):
                callees.append(node.func)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callees.extend(node.decorator_list)
        if not all(callable_at_import(callee) for callee in callees):
            return None
    parts = _PythonParts()
    for node, fields in found:
        flow_id = fields.get("id", "")
        if not flow_id:
            parts.diagnostics.append(
                Diagnostic("error", "flow_invalid", f"{relative}:{node.name} has no id", plugin)
            )
            continue
        parts.flows.append(
            FlowRecord(
                id=flow_id,
                module=relative,
                attr=node.name,
                description=fields.get("description", "") or (_class_doc(node) or "").strip(),
                shape=fields.get("shape", ""),
            )
        )
    return parts


def _python_parts(root: Path, plugin: str, texts: dict[str, str]) -> _PythonParts:
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
            static = _static_flows(path, relative, plugin) if folder == "flows" else None
            if static is not None:
                parts.flows.extend(static.flows)
                parts.diagnostics.extend(static.diagnostics)
                continue
            module = _import(root, relative, plugin, parts)
            if module is None:
                continue
            flow = _flow_base() if folder == "flows" else None
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
                            defer=value.defer,
                            callers=value.callers,
                            permission=value.permission,
                        )
                    )
                elif folder == "tools" and isinstance(value, NativeToolRef):
                    parts.tools.append(
                        ToolRecord(
                            name=value.name,
                            kind="native",
                            defer=value.defer,
                            callers=value.callers,
                            permission=value.permission,
                        )
                    )
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
                    flow is not None
                    and isinstance(value, type)
                    and issubclass(value, flow)
                    and value is not flow
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
    invalid: list[tuple[str, str]] = []
    if described["derive"] not in {"", "caller"}:
        invalid.append(
            (
                "subagent_derive_invalid",
                f"derive must be 'caller' or empty, not {described['derive']!r}",
            )
        )
    elif described["derive"] == "caller" and described["plugins"]:
        invalid.append(
            (
                "subagent_derive_conflict",
                "derives its plugins from the caller and cannot also name plugins",
            )
        )
    if described["effects"] not in {"", "read"}:
        invalid.append(
            (
                "subagent_effects_invalid",
                f"effects must be 'read' or empty, not {described['effects']!r}",
            )
        )
    if invalid:
        parts.diagnostics.extend(
            Diagnostic("error", code, f"subagent {value.id!r}: {message}", plugin)
            for code, message in invalid
        )
        return None
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
        derive=described["derive"],
        effects=described["effects"],
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


@dataclasses.dataclass(frozen=True)
class _StaticParts:
    """What a package's files say without importing its Python (the same trusted or not)."""

    manifest: dict[str, Any]
    plugin: str
    ext: dict[str, Any]
    servers: dict[str, Any]
    host: Any
    exec_digest: str
    text_digest: str
    skills: tuple[SkillRecord, ...]
    fragments: tuple[FragmentRecord, ...]
    texts: dict[str, str]
    commands: tuple[Any, ...]
    workflows: tuple[Any, ...]
    file_hooks: tuple[HookRecord, ...]
    diagnostics: tuple[Diagnostic, ...]
    has_python: bool


#: Within :func:`loading_once`, the folder holding immutable packages and their static parts.
_STATIC_LOADS: contextvars.ContextVar[tuple[str, dict[str, _StaticParts]] | None] = (
    contextvars.ContextVar("clio_plugin_static_loads", default=None)
)


@contextlib.contextmanager
def loading_once(immutable: Path) -> Iterator[None]:
    """Read each package below ``immutable`` once, however many times it loads.

    An install loads a package untrusted, then trusted once the digests allow; only the
    Python parts differ. Below ``immutable`` (the store's content-addressed snapshots,
    never written after their copy) the files read the first time are reused.
    """

    prefix = os.path.join(os.path.abspath(immutable), "")
    token = _STATIC_LOADS.set((prefix, {})) if _STATIC_LOADS.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _STATIC_LOADS.reset(token)


def _static_parts(root: Path) -> _StaticParts:
    memo = _STATIC_LOADS.get()
    key = str(root)
    if memo is not None and key.startswith(memo[0]) and key in memo[1]:
        return memo[1][key]
    static = _read_static_parts(root)
    if memo is not None and key.startswith(memo[0]):
        memo[1][key] = static
    return static


def _read_static_parts(root: Path) -> _StaticParts:
    try:
        manifest, diagnostics = read_manifest(root)
        plugin = manifest["name"]
        raw = dict((manifest.get("extensions") or {}).get(NAMESPACE) or {})
        library = raw if is_library_manifest(raw) else None
        ext = {} if library is not None else clio_extension(manifest, plugin)
    except ManifestError as exc:
        raise PluginLoadError(str(exc)) from exc
    servers, server_diags = read_mcp_config(root, plugin)
    for namespace, connection in (ext.get("connections") or {}).items():
        if namespace in servers:
            raise PluginLoadError(
                f"{plugin}: connection namespace {namespace!r} is already declared"
            )
        servers[namespace] = {"type": "clio-connection", "connection": connection}
    try:
        host = host_semantics(ext, plugin, servers)
    except ManifestError as exc:
        raise PluginLoadError(str(exc)) from exc
    exec_digest, text_digest = tree_digests(root, referenced=referenced_files(root, servers))
    skills, skill_diags = _skills(root, plugin)
    if library is not None:
        mapped, library_diags = library_skills(root, plugin, library)
        known = {skill.id for skill in skills}
        skills += [skill for skill in mapped if skill.id not in known]
        skill_diags += library_diags
    clio_dir = root / NAMESPACE
    fragments, texts, fragment_diags = _fragments_and_texts(clio_dir, plugin)
    commands, command_diags = read_commands(clio_dir / "commands", plugin)
    workflows, workflow_diags = read_workflows(clio_dir / "workflows", plugin)
    command_diags += workflow_diags
    # A native package's hooks/hooks.json is Claude Code's hook file, read the same way.
    from clio_agent.plugins.claude_code import _hooks as hooks_file  # noqa: PLC0415

    file_hooks, hook_diags, _scripts = hooks_file(root, plugin)
    diagnostics += skill_diags + server_diags + fragment_diags + command_diags + hook_diags
    has_python = (
        any((clio_dir / name).is_dir() for name in _PYTHON_DIRS)
        or (clio_dir / "plugin.py").is_file()
    )
    return _StaticParts(
        manifest=manifest,
        plugin=plugin,
        ext=ext,
        servers=servers,
        host=host,
        exec_digest=exec_digest,
        text_digest=text_digest,
        skills=tuple(skills),
        fragments=tuple(fragments),
        texts=texts,
        commands=tuple(commands),
        workflows=tuple(workflows),
        file_hooks=tuple(file_hooks),
        diagnostics=tuple(diagnostics),
        has_python=has_python,
    )


def load_plugin(root: Path, *, marketplace: str = "", trusted: bool) -> PluginRecord:
    """Load the native plugin at ``root``. Raises :class:`PluginLoadError` if fatal."""

    root = root.resolve()
    static = _static_parts(root)
    manifest, plugin = static.manifest, static.plugin
    # Each record owns its tables: a record's consumers may adjust them in place.
    ext = copy.deepcopy(static.ext)
    servers = copy.deepcopy(static.servers)
    host = copy.deepcopy(static.host)
    exec_digest, text_digest = static.exec_digest, static.text_digest
    skills, fragments, texts = static.skills, static.fragments, dict(static.texts)
    commands, workflows, file_hooks = static.commands, static.workflows, static.file_hooks
    diagnostics = list(static.diagnostics)
    has_python = static.has_python
    clio_dir = root / NAMESPACE
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
    from clio_agent.plugins import kernel_bindings  # noqa: PLC0415

    bindings, binding_problems = kernel_bindings.load(root, ext.get("kernel_bindings"), plugin)
    diagnostics += binding_problems
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
        hooks=(*parts.hooks, *file_hooks),
        subagents=tuple(parts.subagents),
        flows=tuple(parts.flows),
        fragments=tuple(fragments),
        commands=tuple(commands),
        workflows=tuple(workflows),
        catalogs=tuple(
            [CatalogRecord(id=str(c), path="") for c in ext.get("catalogs") or ()]
            + _catalogs(clio_dir)
        ),
        workflow_state=workflow_state,
        kernel_bindings=bindings,
        diagnostics=tuple(diagnostics),
        executable_digest=exec_digest,
        text_digest=text_digest,
        keywords=tuple(manifest.get("keywords") or ()),
        category=str(manifest.get("category") or ""),
        python_pending=has_python and not trusted,
        disclosure={key: ext[key] for key in ("defer", "servers") if key in ext},
        follows=str(ext.get("follows") or ""),
        host=host,
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
