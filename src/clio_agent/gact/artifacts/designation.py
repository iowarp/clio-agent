"""Tool-declared designation table — WHICH output paths become artifacts.

Designation, not discovery (owner decision #966.1): a file becomes an artifact by
designation, never by a filesystem scan. This module is the **tool-declared**
channel — the output-path argument names and artifact suffixes a tool's schema
declares, and where such a path lands.

A call runs with the arguments the agent gave: a relative output path is the
server's to resolve, against its own working directory (a stdio server's is the
folder CLIO started it in, the workspace root). The execution boundary binds that
directory for the call (:func:`bind_server_cwd`), so the isolated-node check
(:func:`node_output_refusal`) and the mint (:func:`grounded_output_paths`) resolve a
relative path the same way the server does, and never rewrite it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from clio_agent.gact.artifacts.records import ArtifactKind

# Output-artifact argument names. When a tool writes a deliverable (a plot, an
# export, a report) it takes the destination via one of these args. Models vary
# in whether they emit an ABSOLUTE destination; a relative one lands in the
# server's working directory, the folder CLIO started it in.
OUTPUT_PATH_ARG_NAMES: frozenset[str] = frozenset(
    {
        "output_path",
        "out_path",
        "output_file",
        "outfile",
        "output",
        "save_path",
        "savepath",
        "dest",
        "destination",
        "dest_path",
        "out",
    }
)

# Result-declared output-path keys — the designation-by-RESULT channel (GAP A,
# S5 live gate #971). Some tools carry NO full output path in any arg: they take a
# destination DIRECTORY (``output_dir``) and/or a bare filename (``output_name``),
# derive the concrete path internally, and return it in their structured result.
# ``ndp_stage_resource`` is the canonical case — it stages a download and returns
# the written file as ``local_path`` (no arg names it). These keys name the WRITTEN
# output path in a tool result's ``structuredContent``; a value under one of them
# is a designated generated output subject to the SAME containment + suffix +
# freshness rules as the arg channel. Precision over recall (owner decision
# #966.10): only keys that unambiguously denote a file the tool WROTE — never a
# bare ``path``/``file`` that commonly echoes an INPUT.
RESULT_PATH_KEYS: frozenset[str] = frozenset(
    {
        "local_path",
        "output_path",
        "output_file",
        "saved_path",
        "saved_to",
        "written_path",
        "result_path",
        "out_path",
    }
)

#: Source files of a multi-file document project (a LaTeX paper: its sections, bibliography
#: and local classes). They are tracked like any output, but keyed by their workspace-relative
#: path: ``sections/intro.tex`` in two papers, or two papers' ``main.tex``, are different files.
PROJECT_SOURCE_SUFFIXES: frozenset[str] = frozenset({".tex", ".bib", ".sty", ".cls", ".bst"})

ARTIFACT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
        ".pdf",
        ".gif",
        ".csv",
        ".tsv",
        ".parquet",
        ".json",
        ".html",
        ".txt",
        ".md",
        # #1200: a run's stdout/stderr log fetched off a cluster is a real,
        # citeable output -- the same text a ``.txt`` sibling would be. Its
        # absence here silently dropped every transferred log from the artifact
        # record while the file sat in the workspace.
        ".log",
        ".nc",
        ".h5",
        ".hdf5",
        ".npy",
        ".npz",
        ".xlsx",
        # CAD and mesh files open in the workbench's 3D view.
        ".stl",
        ".step",
        ".stp",
        ".3mf",
        ".obj",
        ".ply",
        ".glb",
        ".gltf",
        *PROJECT_SOURCE_SUFFIXES,
    }
)

# Suffix -> kind mapping for tool-declared artifacts. A designated output path
# whose suffix is absent maps to ``other`` (never guessed from content — the
# model's intent, when it has one, rides ``annotation`` instead).
_SUFFIX_KIND: dict[str, ArtifactKind] = {
    ".png": ArtifactKind.IMAGE,
    ".jpg": ArtifactKind.IMAGE,
    ".jpeg": ArtifactKind.IMAGE,
    ".svg": ArtifactKind.IMAGE,
    ".gif": ArtifactKind.IMAGE,
    ".pdf": ArtifactKind.REPORT,
    ".html": ArtifactKind.REPORT,
    ".md": ArtifactKind.REPORT,
    ".txt": ArtifactKind.REPORT,
    ".log": ArtifactKind.REPORT,
    ".csv": ArtifactKind.DATASET,
    ".tsv": ArtifactKind.DATASET,
    ".parquet": ArtifactKind.DATASET,
    ".nc": ArtifactKind.DATASET,
    ".h5": ArtifactKind.DATASET,
    ".hdf5": ArtifactKind.DATASET,
    ".npy": ArtifactKind.DATASET,
    ".npz": ArtifactKind.DATASET,
    ".xlsx": ArtifactKind.DATASET,
    ".stl": ArtifactKind.MODEL,
    ".step": ArtifactKind.MODEL,
    ".stp": ArtifactKind.MODEL,
    ".3mf": ArtifactKind.MODEL,
    ".obj": ArtifactKind.MODEL,
    ".ply": ArtifactKind.MODEL,
    ".glb": ArtifactKind.MODEL,
    ".gltf": ArtifactKind.MODEL,
    ".json": ArtifactKind.CONFIG,
    ".py": ArtifactKind.SCRIPT,
    ".sh": ArtifactKind.SCRIPT,
}


def kind_for_path(path: str | Path) -> ArtifactKind:
    """Return the artifact kind implied by a path's suffix (``other`` if unknown)."""
    suffix = Path(str(path)).suffix.lower()
    return _SUFFIX_KIND.get(suffix, ArtifactKind.OTHER)


#: The running call's tool and its server's working directory ("" when the server's
#: files are not on this machine, or CLIO knows no working directory of it).
_SERVER_CWD: ContextVar[tuple[str, str] | None] = ContextVar(
    "clio_designation_server_cwd", default=None
)


def bind_server_cwd(tool_name: str, cwd: str) -> None:
    """Bind the working directory of the server the running call goes to.

    Called by the execution boundary in the call's own context, before the call; the
    observer's mint in the same context reads it.
    """

    _SERVER_CWD.set((tool_name, cwd))


def server_cwd(tool_name: str = "") -> str:
    """The bound working directory of ``tool_name``'s server ("" when none is bound)."""

    bound = _SERVER_CWD.get()
    if bound is None or (tool_name and bound[0] != tool_name):
        return ""
    return bound[1]


def resolve_output(value: str, cwd: str) -> Path | None:
    """Where the server writes output path ``value``: as the server resolves it.

    An absolute path is itself; a relative one is joined to the server's working
    directory ``cwd``. ``None`` when ``value`` is relative and ``cwd`` unknown.
    """

    path = Path(value.strip()).expanduser()
    if path.is_absolute():
        return path
    if not cwd:
        return None
    return Path(cwd) / path


def node_output_refusal(tool_name: str, effective_args: Mapping[str, Any]) -> str:
    """Why a call's declared output path may not be written by an isolated node, or ``""``.

    Each output path is resolved as the server resolves it (:func:`resolve_output`,
    against the bound :func:`server_cwd`); one outside the node's write territory is
    refused with the file policy's typed reason, which names the node's folder, before
    the tool runs. The call is never rewritten: the agent picks the path.
    """

    from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError  # noqa: PLC0415

    outputs = {
        key: value
        for key, value in effective_args.items()
        if key in OUTPUT_PATH_ARG_NAMES and isinstance(value, str) and value.strip()
    }
    if not outputs:
        return ""  # nothing to check: the policy (filesystem-resolved roots) is not built
    policy = FileAccessPolicy.from_env()
    if not policy.write_roots:
        return ""
    cwd = server_cwd(tool_name)
    for key, value in outputs.items():
        target = resolve_output(value, cwd)
        if target is None:
            continue  # the server's files are not on this machine
        try:
            policy._ensure_writable(target.resolve(strict=False), field=key)
        except FilePolicyError as refused:
            text = refused.to_text()
            if not Path(value.strip()).expanduser().is_absolute():
                text += (
                    f"\n{key}={value!r} is relative: the server resolves it against its "
                    f"working directory {cwd}."
                )
            return text
    return ""


def grounded_output_paths(effective_args: Mapping[str, Any], tool_name: str = "") -> dict[str, str]:
    """Return the designated output-path args of a call, where the server writes them.

    The tool observer's mint reads this on ``completed``: the subset of the call's
    args that names an output artifact destination (``{arg_name: path}``) whose
    suffix is a recognized artifact suffix, a relative one resolved against the
    running call's server working directory (:func:`server_cwd` of ``tool_name``, or of
    the running call when ``""``). Args whose value is
    not a string, or whose suffix is not an artifact suffix, are excluded (a tool
    taking ``output="stdout"`` mints nothing).
    """

    cwd = server_cwd(tool_name)
    out: dict[str, str] = {}
    for key, value in effective_args.items():
        if key not in OUTPUT_PATH_ARG_NAMES:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if Path(value).suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        target = resolve_output(value, cwd)
        out[key] = str(target) if target is not None else value
    return out


def _structured_result(result: Any) -> dict[str, Any]:
    """Return the structured-content dict of a raw tool result (``{}`` when none).

    A tool result is either the MCP envelope ``{"structuredContent": {...}, ...}``
    or a bare structured dict. Mirrors ``transform_edges._structured_result`` so the
    result-path mint and the authority-edge detector read the SAME shape.
    """
    if not isinstance(result, Mapping):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    return dict(result)


def result_declared_paths(result: Any) -> dict[str, str]:
    """Return the output-path values a tool RESULT declares (designation-by-result).

    The result-path designation channel (GAP A, S5 #971): scan the TOP-LEVEL keys
    of a tool result's structured content for :data:`RESULT_PATH_KEYS`; return the
    subset whose value is a non-empty string naming a recognized artifact suffix as
    ``{result_key: path}``. Bounded — only recognized top-level keys, no recursion,
    no filesystem scan. Containment + existence + freshness are enforced by the mint
    caller (the same guards the arg channel uses), so this returns *candidates*, not
    a mint decision. A value that is not a string, is blank, or carries an
    unrecognized suffix is excluded (a tool returning ``output="stdout"`` yields
    nothing).
    """
    structured = _structured_result(result)
    out: dict[str, str] = {}
    for key, value in structured.items():
        if str(key) not in RESULT_PATH_KEYS:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if Path(value).suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        out[str(key)] = value.strip()
    return out


def pack_declared_paths(
    workflow_state: Mapping[str, Any],
    path_specs: Any,
) -> list[str]:
    """Return the path-string values a plugin schema declares under ``artifact_paths``.

    The pack-declared designation channel (secondary/optional — owner decision
    #966.1): ``path_specs`` is the schema's ``[(section, key), ...]`` list; for
    each, read ``workflow_state[section][key]`` and collect it when it is a
    non-empty string. No filesystem scan, no remote-ref heuristics — just the
    declared fields, in declaration order, de-duplicated.
    """
    out: list[str] = []
    for spec in path_specs or ():
        try:
            section, key = spec
        except (TypeError, ValueError):
            continue
        section_obj = workflow_state.get(section)
        if not isinstance(section_obj, Mapping):
            continue
        token = section_obj.get(key)
        if isinstance(token, str) and token.strip() and token.strip() not in out:
            out.append(token.strip())
    return out
