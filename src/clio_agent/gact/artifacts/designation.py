"""Tool-declared designation table — WHICH output paths become artifacts.

Designation, not discovery (owner decision #966.1): a file becomes an artifact by
designation, never by a filesystem scan. This module is the **tool-declared**
channel — the output-path argument names and artifact suffixes a tool's schema
declares, plus the pre-call grounding that resolves those paths against the bound
workspace root.

These constants + :func:`ground_output_paths` were MOVED here verbatim from
``tools/execution.py`` (issue #966 deletion inventory item 2); ``execution.py``
keeps a thin re-import so the tool boundary's behavior is byte-identical (proven
by ``tests/test_tools/test_execution.py``'s grounding + parity suites). Restated
as the designation table, the same names are what the S1 mint reads to know which
grounded args to hash on tool completion.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.artifacts.records import ArtifactKind

# Output-artifact argument names. When a tool writes a deliverable (a plot, an
# export, a report) it takes the destination via one of these args. Models vary
# in whether they emit an ABSOLUTE destination: stronger models obey the
# "pass an absolute path" prompt, weaker ones emit a bare filename (or omit the
# arg entirely and let the tool's own relative default apply). Either way the
# artifact then lands in the MCP server's CWD instead of the bound workspace,
# where the harness/grader collects deliverables. Grounding these against the
# active workspace root is generic workspace hygiene — it applies to every tool
# and every model, with no per-model or per-tool special-casing.
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
        ".nc",
        ".h5",
        ".hdf5",
        ".npy",
        ".npz",
        ".xlsx",
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
    ".csv": ArtifactKind.DATASET,
    ".tsv": ArtifactKind.DATASET,
    ".parquet": ArtifactKind.DATASET,
    ".nc": ArtifactKind.DATASET,
    ".h5": ArtifactKind.DATASET,
    ".hdf5": ArtifactKind.DATASET,
    ".npy": ArtifactKind.DATASET,
    ".npz": ArtifactKind.DATASET,
    ".xlsx": ArtifactKind.DATASET,
    ".json": ArtifactKind.CONFIG,
    ".py": ArtifactKind.SCRIPT,
    ".sh": ArtifactKind.SCRIPT,
}


def kind_for_path(path: str | Path) -> ArtifactKind:
    """Return the artifact kind implied by a path's suffix (``other`` if unknown)."""
    suffix = Path(str(path)).suffix.lower()
    return _SUFFIX_KIND.get(suffix, ArtifactKind.OTHER)


def is_relative_artifact_path(value: str) -> bool:
    """Return whether a string is a relative path that names a writable file."""

    candidate = value.strip()
    if not candidate:
        return False
    expanded = Path(candidate).expanduser()
    if expanded.is_absolute():
        return False
    # A bare scheme/URL is not a local filesystem destination.
    if "://" in candidate:
        return False
    return True


def schema_properties(input_schema: Any) -> dict[str, Any]:
    """Return the ``properties`` mapping from an MCP tool inputSchema."""

    if not isinstance(input_schema, Mapping):
        return {}
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return dict(properties)


def ground_output_paths(
    args: Mapping[str, Any],
    input_schema: Any,
    workspace_root: str,
) -> dict[str, Any]:
    """Ground tool output-artifact paths against the active workspace root.

    Two model-agnostic repairs, both gated on a bound workspace root:

    1. RESOLVE a relative output path the model EMITS (e.g. ``"plot.png"``)
       against the workspace root so the deliverable lands where the harness
       collects it, instead of in the MCP server's process CWD.
    2. INJECT a workspace-absolute output path when the model OMITS an output
       arg whose schema declares a *relative* default (e.g. plot tools default
       ``output_path="timeseries.png"``). Without this the MCP server applies
       its own relative default inside the server, after this boundary runs.

    Absolute paths the model already supplied are left untouched. No per-model
    or per-tool branches: the only inputs are generic output-arg names and the
    tool's own declared inputSchema.
    """

    grounded = dict(args)
    root = workspace_root.strip()
    if not root:
        return grounded
    root_path = Path(root).expanduser()

    # (1) Resolve relative output paths the model emitted.
    for key, value in list(grounded.items()):
        if key not in OUTPUT_PATH_ARG_NAMES:
            continue
        if not isinstance(value, str) or not is_relative_artifact_path(value):
            continue
        grounded[key] = str(root_path / Path(value.strip()))

    # (2) Inject a workspace-absolute path for omitted output args whose schema
    #     default is relative (the tool would otherwise write to its own CWD).
    properties = schema_properties(input_schema)
    for prop_name, prop_schema in properties.items():
        if prop_name not in OUTPUT_PATH_ARG_NAMES or prop_name in grounded:
            continue
        if not isinstance(prop_schema, Mapping):
            continue
        default = prop_schema.get("default")
        if not isinstance(default, str):
            continue
        default_name = Path(default.strip()).name
        if not default_name:
            continue
        if Path(default_name).suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        if not is_relative_artifact_path(default):
            continue
        grounded[prop_name] = str(root_path / default_name)

    return grounded


def grounded_output_paths(effective_args: Mapping[str, Any]) -> dict[str, str]:
    """Return the designated output-path args present in already-grounded call args.

    The tool observer's mint reads this on ``completed``: given the effective
    (already-:func:`ground_output_paths`-processed) args, return the subset that
    names an output artifact destination — ``{arg_name: absolute_path}`` — whose
    suffix is a recognized artifact suffix. Args whose value is not a string, or
    whose suffix is not an artifact suffix, are excluded (a tool taking
    ``output="stdout"`` mints nothing). Injected schema defaults already present
    in ``effective_args`` are covered since grounding rewrote them in place.
    """

    out: dict[str, str] = {}
    for key, value in effective_args.items():
        if key not in OUTPUT_PATH_ARG_NAMES:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if Path(value).suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        out[key] = value
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
    """Return the path-string values a pack schema declares under ``artifact_paths``.

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
