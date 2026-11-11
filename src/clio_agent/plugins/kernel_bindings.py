"""Kernel bindings: standard-library Python a plugin binds into ``run_code``'s kernel.

A plugin declares them in its extension::

    "ai.iowarp.clio": {"kernel_bindings": {"browser": {
        "source": "ai.iowarp.clio/kernel/browser.py",
        "requires": ["browser_open", "browser_act"]}}}

The kernel runs the source in a module that holds ``tools`` and ``ToolError`` and binds the
module's ``export`` object under the binding's name, so a script writes ``await
browser.open(url)`` instead of raw tool calls. A binding is plain Python that builds tool
calls: it never holds a client, a socket or a credential (browser plan rules 1 and 2).

- The source lives under ``ai.iowarp.clio/``, so the plugin's executable digest covers it
  and a changed binding needs the plugin trusted again (BR21); it is read as text here and
  executed only in the kernel. An untrusted plugin's bindings are not resolved.
- ``plugin check`` compiles each binding and rejects any import outside
  ``sys.stdlib_module_names`` (:func:`diagnostics`): the kernel is standard library only.
- An agent gets a binding only when every tool it requires can be called from a script
  (:func:`attach`), so a denied or removed tool removes the binding with it.
- A binding may name a ``host`` module: plugin Python whose ``@host_call`` functions
  answer the binding's reserved calls (``__<binding>_<name>__``) in the server, without a
  tool call each (the extension point a stream's writes use, CT25 (6)). It is imported
  only for a trusted plugin; a name outside the binding's prefix is not bound.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from clio_agent.plugins.records import Diagnostic, KernelBindingRecord
from clio_agent.runtime import trace

__all__ = ["attach", "check_shape", "diagnostics", "load", "resolution_rows"]

#: Names a plugin binding may not take: the generated ``tools`` binding and the kernel's own.
_RESERVED = frozenset({"tools", "ToolError", "__builtins__", "__name__"})
_PREFIX = "ai.iowarp.clio/"


def check_shape(value: Any, plugin: str) -> None:
    """Validate the manifest's ``kernel_bindings`` object (fatal: the plugin must not load).

    Raises:
        ManifestError: The object is malformed.
    """
    from clio_agent.plugins.manifest import NAMESPACE, ManifestError  # noqa: PLC0415

    if value is None:
        return
    where = f"{plugin}: {NAMESPACE}.kernel_bindings"
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must map binding names to objects")
    for name, entry in value.items():
        if not str(name).isidentifier() or name in _RESERVED:
            raise ManifestError(
                f"{where}.{name}: a binding name is a Python identifier, not reserved"
            )
        if not isinstance(entry, dict) or set(entry) - {"source", "requires", "host"}:
            raise ManifestError(f"{where}.{name} may set only source, requires and host")
        for key in ("source", "host"):
            path = entry.get(key, "" if key == "host" else None)
            if key == "host" and path == "":
                continue
            if not isinstance(path, str) or not path.startswith(_PREFIX) or ".." in path:
                raise ManifestError(f"{where}.{name}.{key} is a path under {_PREFIX}")
        requires = entry.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(t, str) for t in requires):
            raise ManifestError(f"{where}.{name}.requires is a list of tool names")


def load(
    root: Path, value: Any, plugin: str
) -> tuple[tuple[KernelBindingRecord, ...], list[Diagnostic]]:
    """Read each declared binding's source as text (never imported here)."""
    records: list[KernelBindingRecord] = []
    problems: list[Diagnostic] = []
    for name, entry in (value or {}).items():
        path = (root / str(entry["source"])).resolve()
        if not path.is_relative_to(root.resolve()):
            problems.append(
                Diagnostic("error", "kernel_binding_outside", f"{name}: source escapes", plugin)
            )
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(Diagnostic("error", "kernel_binding_missing", f"{name}: {exc}", plugin))
            continue
        records.append(
            KernelBindingRecord(
                name=str(name),
                source=str(entry["source"]),
                text=text,
                digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                requires=tuple(entry.get("requires") or ()),
                host=str(entry.get("host") or ""),
            )
        )
    return tuple(records), problems


def diagnostics(record: Any) -> list[Diagnostic]:
    """Compile each binding and reject imports outside the standard library (``plugin check``)."""
    found: list[Diagnostic] = []
    for binding in getattr(record, "kernel_bindings", ()):
        try:
            tree = ast.parse(binding.text, binding.source)
            compile(tree, binding.source, "exec", dont_inherit=True)
        except SyntaxError as exc:
            found.append(
                Diagnostic(
                    "error",
                    "kernel_binding_syntax",
                    f"{binding.name}: {binding.source}:{exc.lineno}: {exc.msg}",
                    record.id,
                )
            )
            continue
        for module, line in _imports(tree):
            if module.split(".")[0] not in sys.stdlib_module_names:
                found.append(
                    Diagnostic(
                        "error",
                        "kernel_binding_import",
                        f"{binding.name}: {binding.source}:{line} imports {module!r}, which is "
                        "not in the standard library; a kernel binding is standard library only",
                        record.id,
                    )
                )
        if "export" not in {
            t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }:
            found.append(
                Diagnostic(
                    "error",
                    "kernel_binding_no_export",
                    f"{binding.name}: {binding.source} assigns no module-level export",
                    record.id,
                )
            )
    return found


def _imports(tree: ast.AST) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def resolution_rows(records: Iterable[Any]) -> list[dict[str, Any]]:
    """The resolved agent's bindings, from trusted plugins only, in resolution order."""
    rows: list[dict[str, Any]] = []
    for record in records:
        if getattr(record, "python_pending", False):
            continue  # not trusted yet: its binding would run unreviewed code
        for binding in getattr(record, "kernel_bindings", ()):
            rows.append(
                {
                    "name": binding.name,
                    "plugin": record.id,
                    "source": binding.source,
                    "digest": binding.digest,
                    "requires": list(binding.requires),
                    "text": binding.text,
                    "host": getattr(binding, "host", ""),
                    "root": str(getattr(record, "root", "") or ""),
                }
            )
    return rows


def attach(program: Any, resolution: Any) -> None:
    """Bind the agent's script-callable bindings on ``program`` (``_clio_kernel_bindings``)."""
    tools: Mapping[str, Any] = getattr(program, "tools", {}) or {}
    direct_only = frozenset(getattr(program, "_clio_direct_only", ()) or ())
    blocked = frozenset(getattr(program, "_clio_blocked", ()) or ())
    callable_names = {n for n in tools if n not in direct_only and n not in blocked}
    bound: dict[str, dict[str, str]] = {}
    host_calls: dict[str, Any] = {}
    for row in getattr(resolution, "kernel_bindings", None) or ():
        name = str(row.get("name") or "")
        missing = [t for t in row.get("requires") or () if t not in callable_names]
        if "run_code" not in tools:
            missing.append("run_code")
        if missing:
            trace.event("CODE", "kernel_binding_unbound name=%s missing=%s", name, missing)
            continue
        if name in bound:
            trace.event(
                "CODE", "kernel_binding_name_collision name=%s plugin=%s", name, row.get("plugin")
            )
            continue
        bound[name] = {"source": str(row.get("text") or ""), "digest": str(row.get("digest") or "")}
        host_calls.update(_host_calls(name, row))
    object.__setattr__(program, "_clio_kernel_bindings", bound)
    object.__setattr__(program, "_clio_host_calls", host_calls)


def _host_calls(binding: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The ``@host_call`` functions of a bound binding's ``host`` module, by call name."""
    host, root = str(row.get("host") or ""), str(row.get("root") or "")
    if not host or not root:
        return {}
    from clio_agent.plugins.loader import PluginImportError, import_plugin_module  # noqa: PLC0415
    from clio_agent.sdk.tools import HostCallSpec  # noqa: PLC0415

    try:
        module = import_plugin_module(Path(root), host.removeprefix(_PREFIX))
    except PluginImportError as exc:
        trace.event("CODE", "kernel_binding_host_unloaded name=%s error=%s", binding, exc)
        return {}
    calls: dict[str, Any] = {}
    prefix = f"__{binding}_"
    for value in vars(module).values():
        if not isinstance(value, HostCallSpec):
            continue
        if not (value.name.startswith(prefix) and value.name.endswith("__")):
            trace.event("CODE", "host_call_outside_binding name=%s binding=%s", value.name, binding)
            continue
        calls[value.name] = value.function
    return calls
