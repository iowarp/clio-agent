#!/usr/bin/env python3
"""Generate the CLIO_* environment-variable reference from the source tree.

This walks ``src/clio_agent`` with :mod:`ast` and derives two committed,
deterministic artifacts:

* ``docs/ENVIRONMENT.md`` -- a human reference table of every knob.
* ``.env.example`` -- a copy-paste template (values commented out; secrets
  left explicitly blank).

Two kinds of knob are discovered:

1. **File -> env -> default** knobs: every :func:`clio_agent.conf.resolve` call
   (the migrated, sharable configuration surface) -- whether spelled
   ``conf.resolve(...)`` (any module alias) or as a bare ``resolve(...)`` after
   ``from clio_agent.conf import resolve``. For each we record the dotted
   config-file key, the environment variable, the coercion type (from the
   ``cast=`` argument), the in-code default and the defining module. Reads through
   a **single-hop config-resolve helper** are followed too -- a same-module function
   that forwards its ``(key, env, default)`` parameters straight into
   ``conf.resolve`` (e.g. ``_resolve_positive_int("gact.x", "CLIO_X", 2000)`` in
   ``gact/resident_ledgers.py`` / ``gact/runtime/retention.py``). The helper's own
   coercion (an ``as_int`` / ``as_float`` call in its body) supplies the type when
   the wrapped ``conf.resolve`` omits a ``cast=`` argument.
2. **Environment-only** reads of a ``CLIO_*`` / ``ALCF_*`` variable that
   deliberately does *not* go through :mod:`clio_agent.conf` (see that
   module's docstring). Three read shapes are recognised:

   * literal ``os.environ.get`` / ``os.getenv`` calls;
   * single-hop helper wrappers -- a same-module function that forwards a
     parameter straight into an env read (e.g. ``_env_int("CLIO_...", 2000)``
     in ``gact/runtime/retention.py``);
   * injected env mappings that default to the real environment (e.g.
     ``self.env = env if env is not None else os.environ`` in
     ``runtime/status.py``) -- ``.get`` calls on such names count as reads.

   Reads whose variable *name* is computed at runtime (the ``CLIO_CRED_*``
   named-credential family) cannot be AST-discovered and are curated in
   :data:`DYNAMIC_SECRET_VARS`. Each env-only variable is assigned a tier:

   * ``bootstrap`` -- read before the config store exists (would recurse).
   * ``secret`` -- never committed to a shared file; env-only by policy.
   * ``unmigrated`` -- a legacy env read not yet routed through ``conf``.

A small curated ``owned-elsewhere`` tier documents variables consumed outside
``src/clio_agent`` (the ``external/gact-tui`` frontend, the test harness) so the
reference stays honest without inventing or deleting them (#769).

The output is fully deterministic (sorted); ``tests/test_docs/test_env_reference.py``
regenerates in-memory and fails on any drift from the committed files. Run::

    uv run python scripts/gen_env_reference.py          # rewrite artifacts
    uv run python scripts/gen_env_reference.py --check   # verify, no write
"""

from __future__ import annotations

import argparse
import ast
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# The curated operator documentation for ``config.defaults.yaml``: which named
# section each key belongs to, and the one-line note explaining what it does,
# in what unit, and when an operator would change it. Kept in its own module so
# the prose is reviewable on its own and this generator stays mechanical.
# Two import paths are both correct and reach the same module: the file is run
# directly (``python scripts/gen_env_reference.py`` -- ``scripts/`` is sys.path[0])
# and imported by the drift test as ``scripts.gen_env_reference`` (repo root on
# the path, ``scripts`` an implicit namespace package).
try:  # pragma: no cover - exercised by whichever entry point is used
    from scripts.config_key_notes import KEY_NOTES, SECTIONS
except ImportError:  # pragma: no cover
    from config_key_notes import KEY_NOTES, SECTIONS

# --------------------------------------------------------------------------- #
# Location + classification constants
# --------------------------------------------------------------------------- #

SRC_ROOT = "src/clio_agent"
DOC_RELPATH = "docs/ENVIRONMENT.md"
DOTENV_RELPATH = ".env.example"
# Committed, packaged base-layer defaults document. Ships inside the wheel next
# to ``conf.py`` so :class:`clio_agent.conf.ConfigStore` can load it as the
# below-env base layer (see that module's docstring). Every knob with a concrete
# static default is enumerated as a flat dotted-key mapping; dynamic/unset
# defaults are emitted as comments (they resolve to their in-code default).
DEFAULTS_RELPATH = "src/clio_agent/config.defaults.yaml"

# The resolver module itself defines ``conf.resolve``; its docstring examples
# and internal ``self.resolve``/``_STORE.resolve`` calls are not real knobs.
_SKIP_FILES = {"conf.py"}

# Bootstrap tier: read before the config store (or its file discovery) exists,
# so a ``conf.resolve`` here would recurse or read a not-yet-loaded layer.
# (``CLIO_ENV_FILE_LOADED`` is deliberately absent: the dotenv loader only
# *writes* it as a marker; the agent never reads it, so it is not a knob.)
BOOTSTRAP_VARS: frozenset[str] = frozenset({"CLIO_USER_DIR", "CLIO_ENV_FILE"})

# Secret tier: never written to a shared config file; env-only by policy.
SECRET_VARS: frozenset[str] = frozenset(
    {"CLIO_LM_API_KEY", "CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN", "CLIO_RELAY_API_TOKEN"}
)
SECRET_PREFIXES: tuple[str, ...] = ("CLIO_CRED_",)

# Env vars whose *names* are computed at runtime, so no string literal exists
# for the AST walk to find. Curated: pattern -> the module that reads them.
DYNAMIC_SECRET_VARS: dict[str, str] = {
    # providers/credentials.py builds ``CLIO_CRED_<PROVIDER>_<ACCOUNT>`` from
    # the named-credential ref (e.g. ``openai:acctB`` -> CLIO_CRED_OPENAI_ACCTB).
    "CLIO_CRED_<PROVIDER>_<ACCOUNT>": "src/clio_agent/providers/credentials.py",
}

# Variables documented but consumed OUTSIDE ``src/clio_agent`` -- the gact-tui
# frontend or the integration/live test harness own them. Curated (they cannot
# be AST-discovered from the Python source) and marked, never deleted (#769,
# RESOLVED decision 4). Keyed by variable name -> (owner, note).
OWNED_ELSEWHERE: dict[str, tuple[str, str]] = {
    "CLIO_GACT_URL": ("gact-tui", "Base URL the TUI/desktop client dials the gact server on."),
    "CLIO_PORT": ("gact-tui", "Port the desktop supervisor attaches to (raw server uses --port)."),
    "CLIO_SESSION_ID": ("gact-tui", "Session id the client pins a conversation to."),
    "CLIO_WORKSPACE_ID": ("gact-tui", "Workspace id the client scopes requests to."),
    "CLIO_RUN_LIVE": ("test-harness", "Opt-in flag for live-provider integration tests."),
    "CLIO_INTEGRATION_BASE": ("test-harness", "Base URL the contract test suite targets."),
}

# Human-friendly coercion labels keyed by the ``cast=`` callable's bare name
# (matched on the last dotted segment, so ``conf.as_bool`` and an imported
# ``as_bool`` both resolve).
_CAST_TYPES: dict[str, str] = {
    "as_bool": "bool",
    "as_int": "int",
    "as_float": "float",
    "as_csv": "list",
    "as_str": "str",
}

_GENERATED_BANNER = "<!-- GENERATED by scripts/gen_env_reference.py -- DO NOT EDIT. -->"
_GENERATED_DOTENV_BANNER = "# GENERATED by scripts/gen_env_reference.py -- DO NOT EDIT."
_GENERATED_DEFAULTS_BANNER = "# GENERATED by scripts/gen_env_reference.py -- DO NOT EDIT."


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ResolvedVar:
    """A ``conf.resolve`` knob: file -> env -> default.

    ``default`` is the concrete scalar the code falls back to ("" when the
    default is ``None`` / an empty container). ``dynamic_expr`` carries the
    source expression when the default is computed at runtime (e.g.
    ``_default_allowed_roots()``) and therefore has no static scalar.
    """

    env: str
    key: str
    type_: str
    default: str
    source: str
    dynamic_expr: str = ""


@dataclass
class EnvOnlyVar:
    """A bare ``os.environ`` read that stays off the config store."""

    env: str
    tier: str
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfWrapper:
    """A same-module helper forwarding its params into ``conf.resolve``.

    Records which parameter (``(name, index)``) carries the config ``key``, the
    ``env`` var name and the ``default`` at a call site, plus the coercion label the
    helper applies (inferred from its ``cast=`` argument or an ``as_*`` call in its
    body). A call ``_resolve_positive_int("gact.x", "CLIO_X", 2000)`` then resolves
    to a :class:`ResolvedVar` exactly as a direct ``conf.resolve`` would.
    """

    key: tuple[str, int]
    env: tuple[str, int]
    default: tuple[str, int]
    cast: str


# --------------------------------------------------------------------------- #
# AST collection
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


_UNRESOLVED: object = object()

# The subset of binary/unary operators used in in-code default expressions
# (``1 << 30``, ``32 * 1024``, ``-1``). Constant-folded so the reference shows a
# concrete value rather than the source text.
_BIN_OPS: dict[type[ast.operator], object] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
    ast.LShift: lambda a, b: a << b,
    ast.RShift: lambda a, b: a >> b,
    ast.BitOr: lambda a, b: a | b,
    ast.BitAnd: lambda a, b: a & b,
}
_UNARY_OPS: dict[type[ast.unaryop], object] = {
    ast.USub: lambda a: -a,
    ast.UAdd: lambda a: +a,
    ast.Invert: lambda a: ~a,
}


def _eval(node: ast.expr | None, consts: dict[str, object]) -> object:
    """Best-effort constant evaluation; ``_UNRESOLVED`` when not statically known.

    Handles literals, references to module constants, simple int/float
    arithmetic and literal list/tuple containers -- the shapes actually used in
    ``conf.resolve`` ``default=`` arguments and the module constants they name.
    """
    if node is None:
        return _UNRESOLVED
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, _UNRESOLVED)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        operand = _eval(node.operand, consts)
        if operand is _UNRESOLVED or isinstance(operand, (bool, str)):
            return _UNRESOLVED
        return _UNARY_OPS[type(node.op)](operand)  # type: ignore[operator]
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval(node.left, consts)
        right = _eval(node.right, consts)
        if any(v is _UNRESOLVED or isinstance(v, (bool, str)) for v in (left, right)):
            return _UNRESOLVED
        return _BIN_OPS[type(node.op)](left, right)  # type: ignore[operator]
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_eval(e, consts) for e in node.elts]
        if any(v is _UNRESOLVED for v in items):
            return _UNRESOLVED
        return items
    return _UNRESOLVED


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """Map module-level ``NAME = <expr>`` assignments to their evaluated value.

    Captures the constants referenced as ``env=``/``key=``/``default=`` in
    ``conf.resolve`` calls (e.g. ``_REGISTRY_URL_ENV``, ``DEFAULT_AGENT_MAX_STEPS``,
    ``DEFAULT_MAX_FILE_SIZE_BYTES = 1 << 30``). Two passes resolve name chains.
    """
    raw: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    raw[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            raw[node.target.id] = node.value
    consts: dict[str, object] = {}
    for _ in range(2):  # a second pass lets forward name references resolve
        for name, value_node in raw.items():
            resolved = _eval(value_node, consts)
            if resolved is not _UNRESOLVED:
                consts[name] = resolved
    return consts


def _module_path_for(module: str, src: Path) -> Path | None:
    """Return the file backing a dotted ``clio_agent.*`` module name, if any."""
    if not module.startswith("clio_agent."):
        return None
    relative = module.split(".")[1:]
    candidate = src.joinpath(*relative).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = src.joinpath(*relative, "__init__.py")
    return package_init if package_init.is_file() else None


def _imported_constants(tree: ast.Module, src: Path) -> dict[str, object]:
    """Resolve constants this module binds via a MODULE-LEVEL ``from ... import NAME``.

    A shared in-code default is frequently owned by one module and imported by the
    ``conf.resolve`` call site that documents it (``DEFAULT_AUTOCOMPACT_PCT`` lives
    in ``gact/context_preferences_types.py`` and is imported by
    ``gact/runtime/context_tokens.py``). Without this hop such a default renders as
    an opaque ``_(computed)_`` expression and drops out of the committed base layer,
    even though it is perfectly static.

    Deliberately ONE hop and MODULE-LEVEL only (``tree.body``, not ``ast.walk``): the
    binding is then exactly what the importing module holds at import time, with no
    transitive chain to follow and no lazy, function-local import — which may exist to
    break a cycle or to defer a heavy dependency — silently promoted to a static fact.
    Names from outside ``src/clio_agent`` are never resolved.
    """
    out: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        path = _module_path_for(node.module or "", src)
        if path is None:
            continue
        try:
            owner = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        owner_consts = _module_constants(owner)
        for alias in node.names:
            value = owner_consts.get(alias.name, _UNRESOLVED)
            if value is not _UNRESOLVED:
                out[alias.asname or alias.name] = value
    return out


def _class_attr_defaults(tree: ast.Module, consts: dict[str, object]) -> dict[str, object]:
    """Map class-body attribute defaults (``NAME[: ann] = <expr>``) to their value.

    A config-resolve helper frequently defaults a bound to a dataclass field
    (``_resolve_positive_int("gact.x", "CLIO_X", cls.max_bytes)`` where
    ``max_bytes: int = 512 * 1024 * 1024``). Keyed by the bare attribute name so the
    wrapper-default path can resolve a ``cls.<attr>`` / ``self.<attr>`` reference to a
    concrete documented value. Kept separate from :func:`_module_constants` so it is
    consulted ONLY for wrapper defaults — direct ``conf.resolve`` rendering is
    unchanged (no risk of an attribute default resolving differently than before).
    """
    out: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                value = _eval(stmt.value, consts)
                if value is not _UNRESOLVED:
                    out[stmt.target.id] = value
            elif isinstance(stmt, ast.Assign):
                value = _eval(stmt.value, consts)
                if value is _UNRESOLVED:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = value
    return out


def _literal(node: ast.expr | None, consts: dict[str, object]) -> object:
    """Resolve ``node`` to a Python literal, else ``_UNRESOLVED``."""
    return _eval(node, consts)


def _wrapper_default(
    node: ast.expr | None,
    consts: dict[str, object],
    class_attr_consts: dict[str, object],
) -> tuple[str, str]:
    """Return ``(scalar_default, dynamic_expr)`` for a wrapper call's ``default`` arg.

    Like :func:`_render_default`, but a bare ``cls.<attr>`` / ``self.<attr>`` default
    is resolved through the class-attribute defaults so a dataclass-field default
    renders as its concrete value rather than an opaque ``_(computed)_`` expression.
    """
    if isinstance(node, ast.Attribute) and node.attr in class_attr_consts:
        return (_format_value(class_attr_consts[node.attr]), "")
    return _render_default(node, consts)


def _render_default(node: ast.expr | None, consts: dict[str, object]) -> tuple[str, str]:
    """Return ``(scalar_default, dynamic_expr)`` for a ``default=`` value.

    ``scalar_default`` is the concrete fallback ("" for ``None`` / empty
    container); ``dynamic_expr`` is set (and scalar is "") when the default is a
    runtime-computed expression with no static value.
    """
    value = _literal(node, consts)
    if value is _UNRESOLVED:
        return ("", ast.unparse(node) if node is not None else "")
    return (_format_value(value), "")


def _format_value(value: object) -> str:
    """Format a resolved literal as the reference/template shows it."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_format_value(item) for item in value)
    return str(value)


def _cast_type(call: ast.Call) -> str:
    """Return the coercion label from a ``conf.resolve`` call's ``cast=``.

    Matches on the callable's bare name so both ``cast=conf.as_bool`` and an
    imported ``cast=as_bool`` map to the same label.
    """
    for kw in call.keywords:
        if kw.arg == "cast":
            return _CAST_TYPES.get(ast.unparse(kw.value).rsplit(".", 1)[-1], "str")
    return "str"  # no cast -> raw env string / file scalar


def _conf_import_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return ``(resolve_names, conf_aliases)`` bound by this module's imports.

    ``resolve_names`` are bare names that refer to :func:`clio_agent.conf.resolve`
    (``from clio_agent.conf import resolve [as r]``, including function-local
    imports -- the whole tree is walked). ``conf_aliases`` are names bound to
    the conf *module* itself (``from clio_agent import conf [as c]``,
    ``import clio_agent.conf as c``).
    """
    resolve_names: set[str] = set()
    conf_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import clio_agent.conf`` (no asname) binds ``clio_agent``;
                # the ``clio_agent.conf.resolve`` spelling is caught by the
                # ``endswith("conf")`` attribute match. Only asname binds new.
                if alias.name.rsplit(".", 1)[-1] == "conf" and alias.asname:
                    conf_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            from_conf = (node.module or "").rsplit(".", 1)[-1] == "conf"
            for alias in node.names:
                if from_conf and alias.name == "resolve":
                    resolve_names.add(alias.asname or "resolve")
                elif alias.name == "conf":
                    conf_aliases.add(alias.asname or "conf")
    return (resolve_names, conf_aliases)


def _is_conf_resolve(call: ast.Call, resolve_names: set[str], conf_aliases: set[str]) -> bool:
    """True for a call of :func:`clio_agent.conf.resolve` under any spelling.

    Matches ``conf.resolve(...)`` / ``clio_agent.conf.resolve(...)`` (attribute
    call on a conf-module name) and bare ``resolve(...)`` when the module
    imported it from ``clio_agent.conf``.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in resolve_names
    if isinstance(func, ast.Attribute) and func.attr == "resolve":
        base = ast.unparse(func.value)
        return base in conf_aliases or base.endswith("conf")
    return False


def _direct_env_read(call: ast.Call) -> tuple[bool, ast.expr | None]:
    """Detect literal ``os.environ.get`` / ``os.getenv`` reads; return (matched, arg0)."""
    func = call.func
    args = call.args
    if not args:
        return (False, None)
    if isinstance(func, ast.Attribute):
        base = ast.unparse(func.value)
        if func.attr in {"get", "setdefault"} and base.endswith("environ"):
            return (True, args[0])
        if func.attr == "getenv" and base.endswith("os"):
            return (True, args[0])
    return (False, None)


def _env_wrapper_functions(tree: ast.Module) -> dict[str, tuple[str, int]]:
    """Map same-module helper names to the parameter that carries the env-var name.

    A *wrapper* is a function that forwards one of its parameters straight into
    a direct env read (``def _env_int(name, default): os.environ.get(name)``).
    Returns ``{func_name: (param_name, param_index)}`` so call sites like
    ``_env_int("CLIO_LEDGER_...", 2000)`` count as reads of that variable.
    """
    wrappers: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            matched, arg0 = _direct_env_read(inner)
            if matched and isinstance(arg0, ast.Name) and arg0.id in params:
                wrappers[node.name] = (arg0.id, params.index(arg0.id))
                break
    return wrappers


def _conf_resolve_wrappers(
    tree: ast.Module, resolve_names: set[str], conf_aliases: set[str]
) -> dict[str, ConfWrapper]:
    """Map same-module helper names to how they forward params into ``conf.resolve``.

    A *config-resolve wrapper* is a function that calls ``conf.resolve(key, env=env,
    default=default)`` with all three of ``key`` / ``env`` / ``default`` bound to its
    own parameters (single-hop forwarding). Returns ``{func_name: ConfWrapper}`` so a
    call site like ``_resolve_positive_int("gact.x", "CLIO_X", 2000)`` resolves to a
    real knob. The coercion label comes from the wrapped ``conf.resolve``'s ``cast=``
    if present, else an ``as_int`` / ``as_float`` / ... call in the helper body.
    """
    wrappers: dict[str, ConfWrapper] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
        index = {name: pos for pos, name in enumerate(params)}
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if not _is_conf_resolve(inner, resolve_names, conf_aliases):
                continue
            key_node: ast.expr | None = inner.args[0] if inner.args else None
            env_node: ast.expr | None = None
            default_node: ast.expr | None = None
            for kw in inner.keywords:
                if kw.arg == "key":
                    key_node = kw.value
                elif kw.arg == "env":
                    env_node = kw.value
                elif kw.arg == "default":
                    default_node = kw.value
            key_param = _param_ref(key_node, index)
            env_param = _param_ref(env_node, index)
            default_param = _param_ref(default_node, index)
            if key_param and env_param and default_param:
                wrappers[node.name] = ConfWrapper(
                    key=key_param,
                    env=env_param,
                    default=default_param,
                    cast=_wrapper_cast(inner, node),
                )
                break
    return wrappers


def _param_ref(node: ast.expr | None, index: dict[str, int]) -> tuple[str, int] | None:
    """Return ``(name, position)`` if ``node`` names a known parameter, else ``None``."""
    if isinstance(node, ast.Name) and node.id in index:
        return (node.id, index[node.id])
    return None


def _wrapper_cast(call: ast.Call, func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Infer a wrapper's coercion label from its ``conf.resolve`` cast or an ``as_*`` call."""
    cast = _cast_type(call)
    if cast != "str":
        return cast
    for inner in ast.walk(func):
        if isinstance(inner, ast.Call):
            name = ast.unparse(inner.func).rsplit(".", 1)[-1]
            if name in _CAST_TYPES:
                return _CAST_TYPES[name]
    return "str"


def _wrapper_arg(call: ast.Call, param: tuple[str, int]) -> ast.expr | None:
    """Extract a wrapper call's argument for ``param`` (positional or keyword)."""
    name, position = param
    if len(call.args) > position:
        return call.args[position]
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _environ_mapping_aliases(tree: ast.Module) -> set[str]:
    """Names/attributes assigned a mapping that falls back to ``os.environ``.

    Catches the injected-environment pattern (``self.env = env if env is not
    None else os.environ``, ``lookup = env or os.environ``): ``.get`` calls on
    these targets read the real environment by default, so they are env reads.
    Returns the targets' source spelling (``"self.env"``, ``"lookup"``).
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        if not _contains_bare_environ(value):
            continue
        for target in targets:
            if isinstance(target, (ast.Name, ast.Attribute)):
                aliases.add(ast.unparse(target))
    return aliases


def _contains_bare_environ(expr: ast.expr) -> bool:
    """True when ``expr`` references ``os.environ`` as a *mapping* (not a read).

    ``env if env is not None else os.environ`` -> True; a scalar read like
    ``os.environ.get("X")`` -> False (its target holds a value, not the env).
    """
    read_bases: set[int] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"get", "setdefault", "getenv"}:
                read_bases.add(id(node.func.value))
    return any(
        isinstance(node, ast.Attribute) and node.attr == "environ" and id(node) not in read_bases
        for node in ast.walk(expr)
    )


def _env_read_name_node(
    call: ast.Call,
    environ_aliases: set[str],
    wrappers: dict[str, tuple[str, int]],
) -> ast.expr | None:
    """Return the AST node naming the env var ``call`` reads, else ``None``.

    Covers direct ``os.environ``/``os.getenv`` reads, ``.get``/``setdefault``
    on an environ-defaulted mapping alias, and same-module env-read wrappers.
    """
    matched, arg0 = _direct_env_read(call)
    if matched:
        return arg0
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in {"get", "setdefault"} and call.args:
            if ast.unparse(func.value) in environ_aliases:
                return call.args[0]
        if func.attr in wrappers:
            return _wrapper_name_arg(call, wrappers[func.attr])
    elif isinstance(func, ast.Name) and func.id in wrappers:
        return _wrapper_name_arg(call, wrappers[func.id])
    return None


def _wrapper_name_arg(call: ast.Call, param: tuple[str, int]) -> ast.expr | None:
    """Extract the env-var-name argument from a wrapper call (positional or keyword)."""
    param_name, index = param
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == param_name:
            return kw.value
    return None


def _is_tracked_var(name: object) -> bool:
    """True for a ``CLIO_*`` / ``ALCF_*`` string literal."""
    return isinstance(name, str) and (name.startswith("CLIO_") or name.startswith("ALCF_"))


def _classify_tier(var: str) -> str:
    """Assign an env-only tier to ``var``."""
    if var in BOOTSTRAP_VARS:
        return "bootstrap"
    if var in SECRET_VARS or any(var.startswith(p) for p in SECRET_PREFIXES):
        return "secret"
    return "unmigrated"


def collect(root: Path | None = None) -> tuple[list[ResolvedVar], list[EnvOnlyVar]]:
    """Walk the source tree and return (resolved knobs, env-only vars).

    Both lists are sorted by variable name for deterministic output. Variables
    resolved through ``conf`` are removed from the env-only set (a ``conf.resolve``
    knob commonly keeps a bare-env legacy fallback; the resolved entry is
    authoritative).
    """
    repo_root = root or _repo_root()
    src = repo_root / SRC_ROOT
    resolved: dict[str, ResolvedVar] = {}
    env_only: dict[str, EnvOnlyVar] = {}

    for path in sorted(src.rglob("*.py")):
        if path.name in _SKIP_FILES:
            continue
        rel = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Own module-level constants win over an imported binding of the same name.
        consts = {**_imported_constants(tree, src), **_module_constants(tree)}
        class_attr_consts = {**consts, **_class_attr_defaults(tree, consts)}
        resolve_names, conf_aliases = _conf_import_aliases(tree)
        wrappers = _env_wrapper_functions(tree)
        conf_wrappers = _conf_resolve_wrappers(tree, resolve_names, conf_aliases)
        environ_aliases = _environ_mapping_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_conf_resolve(node, resolve_names, conf_aliases):
                _record_resolve(node, consts, rel, resolved)
                continue
            if isinstance(node.func, ast.Name) and node.func.id in conf_wrappers:
                _record_wrapper_resolve(
                    node, conf_wrappers[node.func.id], consts, class_attr_consts, rel, resolved
                )
                continue
            arg0 = _env_read_name_node(node, environ_aliases, wrappers)
            if arg0 is not None:
                name = _literal(arg0, consts)
                if _is_tracked_var(name):
                    var = str(name)
                    entry = env_only.setdefault(var, EnvOnlyVar(var, _classify_tier(var)))
                    if rel not in entry.sources:
                        entry.sources.append(rel)

    # Dynamically-named reads (no literal for the walk to find) are curated.
    for var, source in DYNAMIC_SECRET_VARS.items():
        entry = env_only.setdefault(var, EnvOnlyVar(var, "secret"))
        if source not in entry.sources:
            entry.sources.append(source)

    # Resolved knobs win over any bare-env legacy fallback of the same var.
    for var in resolved:
        env_only.pop(var, None)

    resolved_list = sorted(resolved.values(), key=lambda r: r.env)
    env_list = sorted(env_only.values(), key=lambda e: e.env)
    for entry in env_list:
        entry.sources.sort()
    return (resolved_list, env_list)


def _record_resolve(
    call: ast.Call, consts: dict[str, object], rel: str, out: dict[str, ResolvedVar]
) -> None:
    """Extract a ResolvedVar from a ``conf.resolve`` call into ``out``."""
    key_node: ast.expr | None = call.args[0] if call.args else None
    env_node: ast.expr | None = None
    default_node: ast.expr | None = None
    for kw in call.keywords:
        if kw.arg == "key":
            key_node = kw.value
        elif kw.arg == "env":
            env_node = kw.value
        elif kw.arg == "default":
            default_node = kw.value

    env_val = _literal(env_node, consts)
    if not _is_tracked_var(env_val):
        return  # can't resolve the env name to a CLIO_*/ALCF_* literal
    env = str(env_val)
    key_val = _literal(key_node, consts)
    key = str(key_val) if isinstance(key_val, str) else ""
    default, dynamic_expr = _render_default(default_node, consts)
    record = ResolvedVar(
        env=env,
        key=key,
        type_=_cast_type(call),
        default=default,
        source=rel,
        dynamic_expr=dynamic_expr,
    )
    # First definition (sorted walk is stable) wins; keep it deterministic.
    out.setdefault(env, record)


def _record_wrapper_resolve(
    call: ast.Call,
    wrapper: ConfWrapper,
    consts: dict[str, object],
    class_attr_consts: dict[str, object],
    rel: str,
    out: dict[str, ResolvedVar],
) -> None:
    """Extract a ResolvedVar from a config-resolve wrapper call site into ``out``."""
    env_val = _literal(_wrapper_arg(call, wrapper.env), consts)
    if not _is_tracked_var(env_val):
        return  # can't resolve the env name to a CLIO_*/ALCF_* literal
    env = str(env_val)
    key_val = _literal(_wrapper_arg(call, wrapper.key), consts)
    key = str(key_val) if isinstance(key_val, str) else ""
    default, dynamic_expr = _wrapper_default(
        _wrapper_arg(call, wrapper.default), consts, class_attr_consts
    )
    record = ResolvedVar(
        env=env,
        key=key,
        type_=wrapper.cast,
        default=default,
        source=rel,
        dynamic_expr=dynamic_expr,
    )
    out.setdefault(env, record)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_markdown(resolved: list[ResolvedVar], env_only: list[EnvOnlyVar]) -> str:
    """Render ``docs/ENVIRONMENT.md``."""
    lines: list[str] = [
        _GENERATED_BANNER,
        "",
        "# Environment variable reference",
        "",
        "Every `CLIO_*` / `ALCF_*` variable the agent reads, derived from the "
        "source tree by `scripts/gen_env_reference.py`. Discovery covers "
        "`conf.resolve(...)` calls (module-qualified or imported), single-hop "
        "config-resolve helpers (a function forwarding its `key`/`env`/`default` "
        "into `conf.resolve`, e.g. `_resolve_positive_int(...)`), literal "
        "`os.environ` / `os.getenv` reads, single-hop env-read helpers (e.g. "
        '`_env_int("CLIO_...")`), and injected env mappings that default to '
        "`os.environ`; dynamically named variables (`CLIO_CRED_*`) and "
        "variables consumed outside the Python tree are curated. Regenerate "
        "after adding or renaming a knob; "
        "`tests/test_docs/test_env_reference.py` fails on drift.",
        "",
        "## Configured knobs (config file -> env -> default)",
        "",
        "These resolve through `clio_agent.conf`: a value under the dotted key in "
        "`config.yaml` wins, else the environment variable, else the in-code "
        "default. See `src/clio_agent/conf.py` for the precedence rationale.",
        "",
        "| Environment variable | Config key | Type | Default | Defined in |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in resolved:
        if r.dynamic_expr:
            default = f"`{r.dynamic_expr}` _(computed)_"
        elif r.default != "":
            default = f"`{r.default}`"
        else:
            default = "_(unset)_"
        key = f"`{r.key}`" if r.key else "_(dynamic)_"
        lines.append(f"| `{r.env}` | {key} | {r.type_} | {default} | `{r.source}` |")

    lines += [
        "",
        "### Codex SDK transport",
        "",
        "Codex runs through the official Python SDK and its venv-bundled binary, "
        "with one SDK-owned cancellation path. This deliberately replaces the "
        "former stateful-delta/subprocess transport in exchange for an observed "
        "roughly 2.5x time-to-first-token cost. "
        "`CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S` is a progress deadline: it resets "
        "after every SDK event rather than imposing a fixed wall-clock cap on a "
        "healthy long-running exchange.",
        "",
        "## Environment-only variables",
        "",
        "These deliberately bypass the config store (a shared file must not be "
        "able to redirect them). `bootstrap` = read before the store exists; "
        "`secret` = never committed to a shared file; `unmigrated` = a legacy "
        "env read not yet routed through `conf`.",
        "",
        "| Environment variable | Tier | Read in |",
        "| --- | --- | --- |",
    ]
    for e in env_only:
        sources = ", ".join(f"`{s}`" for s in e.sources)
        lines.append(f"| `{e.env}` | {e.tier} | {sources} |")

    lines += [
        "",
        "## Owned elsewhere",
        "",
        "Documented variables consumed *outside* `src/clio_agent` -- the "
        "`external/gact-tui` frontend or the test harness. Listed for "
        "completeness; the Python agent does not read them.",
        "",
        "| Environment variable | Owner | Note |",
        "| --- | --- | --- |",
    ]
    for var in sorted(OWNED_ELSEWHERE):
        owner, note = OWNED_ELSEWHERE[var]
        lines.append(f"| `{var}` | {owner} | {note} |")

    lines.append("")
    return "\n".join(lines)


def render_dotenv(resolved: list[ResolvedVar], env_only: list[EnvOnlyVar]) -> str:
    """Render ``.env.example`` (all knobs commented; secrets left blank)."""
    lines: list[str] = [
        _GENERATED_DOTENV_BANNER,
        "# Copy to .env and uncomment the knobs you want to override.",
        "# Precedence is config.yaml > this env layer > in-code default.",
        "",
        "# --- Configured knobs (uncomment to override the in-code default) ---",
    ]
    for r in resolved:
        lines.append(f"# {r.env}={r.default}")

    secrets = [e for e in env_only if e.tier == "secret"]
    bootstrap = [e for e in env_only if e.tier == "bootstrap"]
    unmigrated = [e for e in env_only if e.tier == "unmigrated"]

    if secrets:
        lines += [
            "",
            "# --- Secrets (env-only; never commit real values) ---",
        ]
        for e in secrets:
            if "<" in e.env:
                # Dynamic name pattern (e.g. CLIO_CRED_<PROVIDER>_<ACCOUNT>):
                # a placeholder is not a valid assignment, keep it commented.
                lines.append(f"# {e.env}=")
            else:
                lines.append(f"{e.env}=")

    if bootstrap:
        lines += [
            "",
            "# --- Bootstrap (read before the config store; env-only) ---",
        ]
        for e in bootstrap:
            lines.append(f"# {e.env}=")

    if unmigrated:
        lines += [
            "",
            "# --- Environment-only (not resolved through config.yaml) ---",
        ]
        for e in unmigrated:
            lines.append(f"# {e.env}=")

    lines.append("")
    return "\n".join(lines)


def _yaml_scalar(type_: str, default: str) -> str:
    """Render a resolved knob's concrete default as a YAML scalar.

    ``default`` is the generator's already-formatted fallback (``_format_value``:
    ``true``/``false`` for bools, decimal text for numbers, ``a,b`` for lists,
    the raw text for strings). The YAML rendering must round-trip through the
    knob's ``conf.as_*`` cast back to the in-code default, so string values are
    always quoted (keeps ``"0.85"`` / ``"50GB"`` strings, not YAML numbers) and
    booleans/numbers are emitted bare.
    """
    if type_ == "bool":
        return default  # already "true"/"false"
    if type_ in {"int", "float"}:
        return default  # decimal text parses as a YAML number
    if type_ == "list":
        parts = [part for part in default.split(",") if part]
        return "[" + ", ".join(_yaml_quote(part) for part in parts) + "]"
    return _yaml_quote(default)


def _yaml_quote(value: str) -> str:
    """Double-quote a string value for YAML, escaping backslashes and quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_SECTION_RULE = "# " + "=" * 74


def section_for(key: str) -> str:
    """Return the curated section name owning ``key``, or ``""`` when unassigned.

    Matched on the key's FIRST dotted segment against each section's prefix
    tuple, in :data:`SECTIONS` order. An unassigned key is a documentation gap
    the drift test fails on rather than a rendering decision made here.
    """
    head = key.split(".")[0]
    for name, prefixes, _description in SECTIONS:
        if head in prefixes:
            return name
    return ""


def _defaults_entry_lines(record: ResolvedVar) -> list[str]:
    """Render one knob: provenance header, curated note, then the key line."""
    lines = [f"# {record.env}  ({record.type_})  <- {record.source}"]
    note = KEY_NOTES.get(record.key, "")
    if note:
        lines.extend(f"# {piece}" for piece in textwrap.wrap(note, width=74))
    if record.dynamic_expr:
        lines.append(f"# {record.key}:  # computed at runtime: {record.dynamic_expr}")
    elif record.default == "":
        lines.append(f"# {record.key}:  # default unset; resolved in code")
    else:
        lines.append(f"{record.key}: {_yaml_scalar(record.type_, record.default)}")
    lines.append("")
    return lines


def render_defaults_yaml(resolved: list[ResolvedVar]) -> str:
    """Render the committed ``src/clio_agent/config.defaults.yaml`` base layer.

    A flat, dotted-key YAML mapping of every ``conf.resolve`` knob to its in-code
    default, each preceded by a comment carrying the environment override name,
    the coercion type and the defining module, plus the curated one-line note
    from :data:`KEY_NOTES` (what it does, its unit, when an operator changes it).
    Knobs whose default is computed at runtime (``dynamic_expr``) or unset
    (empty) are emitted as *comments* only — they carry no value, so
    :meth:`clio_agent.conf.ConfigStore.resolve` falls through to the operative
    in-code ``default=`` for them. The keys are literal dotted strings
    (``arc.store: cte``) matched by exact key in the below-env defaults layer,
    deliberately NOT the nested shape ``config.yaml`` uses.

    Keys are grouped into the named :data:`SECTIONS` and sorted alphabetically
    WITHIN each section. Grouping is a readability decision only and is safe to
    change: the sole runtime consumer is ``ConfigStore.resolve``'s exact-key
    ``dict.get`` on the parsed mapping, which never iterates the document, so no
    behaviour depends on key order.
    """
    lines: list[str] = [
        _GENERATED_DEFAULTS_BANNER,
        "#",
        "# Committed base-layer defaults for clio-agent, loaded by",
        "# clio_agent.conf.ConfigStore as the layer BELOW the environment and",
        "# above the in-code default (which stays the operative fallback). Every",
        "# key mirrors an in-code conf.resolve default; a drift test regenerates",
        "# this file and fails CI if they diverge. Comment lines carry the env",
        "# override name, the coercion type, the defining module and a one-line",
        "# note on what the knob does and when to change it. Keys are grouped",
        "# into named sections purely for readability -- the runtime looks each",
        "# one up by exact key and never iterates, so order carries no meaning.",
        "# Regenerate with: uv run python scripts/gen_env_reference.py",
        "",
    ]
    by_section: dict[str, list[ResolvedVar]] = {name: [] for name, _, _ in SECTIONS}
    unassigned: list[ResolvedVar] = []
    for record in resolved:
        if not record.key:
            continue
        section = section_for(record.key)
        (by_section[section] if section else unassigned).append(record)

    for name, _prefixes, description in SECTIONS:
        members = sorted(by_section[name], key=lambda r: r.key)
        if not members:
            continue
        lines += [_SECTION_RULE, f"# {name}", _SECTION_RULE]
        lines += [f"# {piece}" for piece in textwrap.wrap(description, width=74)]
        lines.append("")
        for record in members:
            lines += _defaults_entry_lines(record)

    if unassigned:
        # A knob under a brand-new top-level namespace. Emitted (never dropped)
        # under an explicit heading; the drift test fails until it is filed.
        lines += [_SECTION_RULE, "# Unassigned -- add these to scripts/config_key_notes.py", ""]
        for record in sorted(unassigned, key=lambda r: r.key):
            lines += _defaults_entry_lines(record)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def generate(root: Path | None = None) -> tuple[str, str]:
    """Return the (markdown, dotenv) text for the current source tree."""
    resolved, env_only = collect(root)
    return (render_markdown(resolved, env_only), render_dotenv(resolved, env_only))


def generate_defaults(root: Path | None = None) -> str:
    """Return the ``config.defaults.yaml`` text for the current source tree."""
    resolved, _ = collect(root)
    return render_defaults_yaml(resolved)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. ``--check`` verifies without writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed artifacts match; exit 1 on drift (no write).",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    markdown, dotenv = generate(repo_root)
    defaults = generate_defaults(repo_root)
    doc_path = repo_root / DOC_RELPATH
    dotenv_path = repo_root / DOTENV_RELPATH
    defaults_path = repo_root / DEFAULTS_RELPATH

    if args.check:
        drift = []
        for path, want in (
            (doc_path, markdown),
            (dotenv_path, dotenv),
            (defaults_path, defaults),
        ):
            have = path.read_text(encoding="utf-8") if path.is_file() else ""
            if have != want:
                drift.append(path.relative_to(repo_root).as_posix())
        if drift:
            print("FAIL: env reference drift in: " + ", ".join(drift))
            print("Run: uv run python scripts/gen_env_reference.py")
            return 1
        print("OK: env reference matches the source tree.")
        return 0

    doc_path.write_text(markdown, encoding="utf-8")
    dotenv_path.write_text(dotenv, encoding="utf-8")
    defaults_path.write_text(defaults, encoding="utf-8")
    print(f"Wrote {DOC_RELPATH}, {DOTENV_RELPATH} and {DEFAULTS_RELPATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
