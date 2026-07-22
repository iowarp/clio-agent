"""Deterministic reproduction renderer — ``reproduce.py`` from a lineage chain (S7 #973).

The owner extension to S7 (2026-07-22): the RO-Crate export gains a compiled
``reproduce.py`` (and a notebook-staged variant) generated from the artifact
lineage. Transforms are emitted in topological order; each instrument is
translated via a per-tool **translation registry** into its plain-tool equivalent
(a staged download → ``requests.get`` of the recorded source url; a pandas filter
→ the pandas expression from the recorded args; a timeseries plot → the
matplotlib call; model-designated inline content → a write-these-bytes stage from
the exported CAS bytes), and every stage ends with an executable
``assert sha256(output) == <recorded pin>`` — the provenance checks become runtime
assertions.

**Per-stage honesty** (the existing vocabularies decide, never a blanket claim):

* :attr:`StageVerdict.DETERMINISTIC` — a registry translation exists, every used
  input is content-pinned (hash-pair / harness-pinned), AND the environment tier
  permits (``>= lockfile-hash``). Copy-paste; a bit-identical replay is asserted.
* :attr:`StageVerdict.WRITE_BYTES` — model-designated inline content: no producing
  tool to translate, but the exact bytes are pinned (exported from CAS). The stage
  writes those recorded bytes and asserts the sha — deterministic by construction.
* :attr:`StageVerdict.RE_RUNNABLE` — a translation exists but a precondition fails
  (an input is authority-pinned / unpinned, or the env tier is below
  ``lockfile-hash``): the code runs and the sha assert still guards, but a
  bit-identical result is NOT guaranteed (a staged download's remote is mutable; a
  raster plot depends on the rendering stack).
* :attr:`StageVerdict.AGENTIC_ONLY` — no registry translation for the tool: the
  stage emits the MCP invocation form (tool + recorded args) as a non-executable
  reference, marked agentic-only (re-hand the recorded task to a live agent).
* :attr:`StageVerdict.GAP_BREAK` — the output is a ``gap`` version (mechanism
  ``none`` — an undesignated overwrite, unknown producer): an explicit ``raise``
  break; the chain cannot be reproduced past this point.

Chains that do not fully qualify still export — the script says which stages are
copy-paste and which are not. The AGENTIC replay runner (re-handing the task to a
live agent) is deliberately out of scope (a session-runtime feature, post-campaign).

Pure + dependency-free: consumes plain :class:`TransformRecord` values and
resolved :class:`ArtifactNode` descriptors (built by :mod:`export` from the
registry), returns text. No app state, no I/O.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from clio_agent.gact.artifacts.environment import EnvironmentRecord, EnvironmentTier, tier_at_least
from clio_agent.gact.artifacts.transform_types import EdgeEvidence
from clio_agent.gact.artifacts.transforms import TransformRecord


class StageVerdict(str, Enum):
    """The honest per-stage reproduction verdict (owner extension #973)."""

    DETERMINISTIC = "deterministic"
    WRITE_BYTES = "write-bytes"
    RE_RUNNABLE = "re-runnable"
    AGENTIC_ONLY = "agentic-only"
    GAP_BREAK = "gap-break"


@dataclass(frozen=True)
class ArtifactNode:
    """A registry version resolved for the renderer (built by :mod:`export`)."""

    artifact_id: str
    name: str
    version: int
    sha256: Optional[str]
    kind: str
    custody: str
    mechanism: str
    path: str
    #: The crate-relative path where the bytes were exported (``""`` when the bytes
    #: were not shipped — e.g. an over-threshold referenced version).
    bundle_path: str = ""


@dataclass
class CompiledStage:
    """One compiled reproduction stage."""

    index: int
    call_id: str
    tool: str
    verdict: StageVerdict
    reason: str
    code: list[str] = field(default_factory=list)
    output_names: list[str] = field(default_factory=list)


@dataclass
class ReproduceScript:
    """The compiled script text + per-stage metadata."""

    text: str
    stages: list[CompiledStage]

    @property
    def deterministic_stages(self) -> int:
        return sum(
            1
            for s in self.stages
            if s.verdict in (StageVerdict.DETERMINISTIC, StageVerdict.WRITE_BYTES)
        )

    @property
    def agentic_only_stages(self) -> int:
        return sum(1 for s in self.stages if s.verdict is StageVerdict.AGENTIC_ONLY)

    @property
    def gap_stages(self) -> int:
        return sum(1 for s in self.stages if s.verdict is StageVerdict.GAP_BREAK)


# --------------------------------------------------------------------------- #
# Translation registry — per-tool instrument → plain-tool code.
# --------------------------------------------------------------------------- #


def _raise_line(message: str) -> str:
    """A syntactically-safe ``raise SystemExit(<message>)`` source line.

    The message is emitted via ``repr`` so any quotes/backslashes in an artifact
    name or tool id are escaped — the generated script must always be valid Python.
    """
    return f"raise SystemExit({message!r})"


def _pick(args: dict, *keys: str) -> str:
    """First non-empty string value among ``keys`` in ``args`` (else ``""``)."""
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _primary_output(outputs: list[ArtifactNode]) -> Optional[ArtifactNode]:
    """The output a stage's sha-assert targets (the first shipped/hashed output)."""
    for node in outputs:
        if node.sha256:
            return node
    return outputs[0] if outputs else None


# Translator kinds — the plain-tool family a recognized tool maps to.
_TR_DOWNLOAD = "download"
_TR_PANDAS = "pandas"
_TR_PLOT = "plot"

#: EXACT tool-name → translator kind (frozen; NO substring matching — finding [3]).
#: Substring dispatch mis-routed unrelated tools (``reconfigure_workspace`` matched
#: ``figure``; ``get_stage_status`` matched ``stage``) and stamped them with a false
#: verdict + an un-passable sha assert. An unrecognized tool is AGENTIC-ONLY, never
#: mis-translated — precision over recall (owner decision #966.10). Add a tool here
#: (with a verified translator) rather than widen the match.
_TOOL_TRANSLATORS: dict[str, str] = {
    "ndp_stage_resource": _TR_DOWNLOAD,
    "stage_resource": _TR_DOWNLOAD,
    "pandas_filter_data": _TR_PANDAS,
    "plot_plot_timeseries": _TR_PLOT,
    "plot_timeseries": _TR_PLOT,
}


def _translate_stage(
    tool: str, args: dict, inputs: list[ArtifactNode], outputs: list[ArtifactNode]
) -> tuple[StageVerdict, str, list[str], bool]:
    """Translate a tool call to code + a base verdict (before pin/env gating).

    Returns ``(verdict, reason, code_lines, sha_assertable)``. Dispatch is by EXACT
    tool name (:data:`_TOOL_TRANSLATORS`), never a substring. The verdict here reflects
    only *translatability*; :func:`compile_reproduce` downgrades a DETERMINISTIC stage
    to RE_RUNNABLE when an input is not content-pinned or the env tier is too weak.
    ``sha_assertable`` is ``False`` when the emitted code does NOT actually write the
    output (an MCP-invocation reference), so no sha assert it can't back is appended.

    SECURITY: every recorded arg value interpolated into the generated source goes
    through ``repr`` / ``json.dumps`` — model/data-derived text (a query expression, a
    url, a column name) can never break out of its string literal into live code.
    """
    kind = _TOOL_TRANSLATORS.get(tool.strip().lower())
    out = _primary_output(outputs)
    out_path = out.name if out is not None else "output.bin"

    # ---- staged download (ndp_stage_resource / stage_resource) --------------
    if kind == _TR_DOWNLOAD:
        url = _pick(args, "source_url", "url", "resource_url", "metadata_source_url", "href")
        if url:
            code = [
                "import requests",
                f"_url = {url!r}",
                f"_out = {out_path!r}",
                "with requests.get(_url, timeout=120, stream=True) as _r:",
                "    _r.raise_for_status()",
                "    with open(_out, 'wb') as _f:",
                "        for _chunk in _r.iter_content(1 << 20):",
                "            _f.write(_chunk)",
            ]
            # A staged download's input is a mutable remote locator (authority),
            # never bit-pinned — re-runnable, but the output sha assert guards it.
            return StageVerdict.RE_RUNNABLE, "staged_download_authority_input", code, True
        return StageVerdict.AGENTIC_ONLY, "stage_missing_source_url", [], False

    # ---- pandas filter (pandas_filter_data) ---------------------------------
    if kind == _TR_PANDAS:
        in_path = inputs[0].name if inputs else _pick(args, "input_path", "path", "csv")
        expr = _pick(args, "expression", "query", "pandas_expression", "where")
        # DETERMINISTIC only when the recorded args match the query-expression shape
        # the translator ACTUALLY reproduces (finding [9]). A structured filter DSL
        # (``filter_conditions`` dict, etc.) is NOT that shape — dropping it silently
        # and stamping DETERMINISTIC would ship an un-passable sha assert, so the
        # stage becomes a re-runnable MCP reference with NO sha assert.
        if in_path and expr:
            code = [
                "import pandas as pd",
                f"_df = pd.read_csv({in_path!r})",
                f"_df = _df.query({expr!r})  # recorded filter expression",
            ]
            columns = args.get("columns")
            if isinstance(columns, list) and columns:
                code.append(f"_df = _df[{list(columns)!r}]  # recorded column projection")
            code.append(f"_df.to_csv({out_path!r}, index=False)")
            return StageVerdict.DETERMINISTIC, "", code, True
        return (
            StageVerdict.RE_RUNNABLE,
            "pandas_filter_shape_not_reproduced",
            _mcp_reference(
                tool,
                args,
                outputs,
                reason="pandas_filter_shape_not_reproduced",
                label="NOT REPRODUCED",
            ),
            False,
        )

    # ---- timeseries plot (plot_plot_timeseries / plot_timeseries) -----------
    if kind == _TR_PLOT:
        in_path = inputs[0].name if inputs else _pick(args, "input_path", "path", "csv")
        if in_path:
            x = _pick(args, "x", "x_col", "time_col") or "index"
            y = _pick(args, "y", "y_col", "value_col")
            code = [
                "import pandas as pd",
                "import matplotlib",
                "matplotlib.use('Agg')",
                "import matplotlib.pyplot as plt",
                f"_df = pd.read_csv({in_path!r})",
                "_fig, _ax = plt.subplots()",
            ]
            if y:
                x_arg = f"_df[{x!r}]" if x != "index" else "_df.index"
                code.append(f"_ax.plot({x_arg}, _df[{y!r}])")
            else:
                code.append("_df.plot(ax=_ax)")
            code.append(f"_fig.savefig({out_path!r})")
            # A raster plot's bytes depend on the matplotlib/freetype stack even
            # with pinned inputs — translatable but re-runnable, not bit-identical.
            return StageVerdict.RE_RUNNABLE, "raster_render_env_sensitive", code, True
        return StageVerdict.AGENTIC_ONLY, "plot_missing_input", [], False

    # ---- unknown tool -> agentic-only reference -----------------------------
    return StageVerdict.AGENTIC_ONLY, "no_registry_translation", [], False


def _mcp_reference(
    tool: str,
    args: dict,
    outputs: list[ArtifactNode],
    *,
    reason: str,
    label: str = "AGENTIC-ONLY",
) -> list[str]:
    """A non-executable MCP-invocation reference (recorded tool + args) for a stage.

    Shared by AGENTIC-ONLY stages (no translation) and a translatable tool whose
    recorded args do NOT match the reproduced shape (finding [9], ``label`` =
    ``NOT REPRODUCED``). The recorded args are serialized via ``json.dumps`` (safe,
    never interpolated as code) into a comment block, then a ``raise SystemExit``
    breaks the chain deterministically — the stage cannot be reproduced here, so it
    never silently produces wrong bytes.
    """
    out = _primary_output(outputs)
    out_name = out.name if out is not None else "(no output)"
    try:
        args_repr = json.dumps(args, default=str, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        args_repr = repr(args)
    lines = [
        f"# {label} ({reason}): re-run tool {tool!r} via its MCP invocation.",
        "# Re-hand the recorded task to a live agent, or invoke the MCP tool directly:",
        f"#   tool: {tool}",
    ]
    lines.extend(f"#   {line}" for line in f"args: {args_repr}".splitlines())
    lines.append(f"#   -> produces: {out_name}")
    lines.append(
        _raise_line(
            f"[reproduce] {reason}: cannot reproduce {out_name} deterministically "
            f"(tool {tool}); re-run it via its MCP invocation"
        )
    )
    return lines


# --------------------------------------------------------------------------- #
# Topological ordering + compilation.
# --------------------------------------------------------------------------- #


def _topological_order(transforms: list[TransformRecord]) -> list[TransformRecord]:
    """Order transforms so a producer precedes any consumer of its outputs.

    Kahn's algorithm over the used→generated dependency: transform ``T`` depends on
    ``U`` iff some ``T.used`` artifact_id is a ``U.generated`` artifact_id. Ties (and
    a cyclic residue — provenance should be acyclic, but never hang) break by
    ``started_at`` then ``call_id``, deterministic + total.
    """
    produced_by: dict[str, str] = {}
    for t in transforms:
        for edge in t.generated:
            if edge.artifact_id:
                produced_by[edge.artifact_id] = t.call_id
    by_id = {t.call_id: t for t in transforms}
    deps: dict[str, set[str]] = {t.call_id: set() for t in transforms}
    for t in transforms:
        for edge in t.used:
            producer = produced_by.get(edge.artifact_id)
            if producer and producer != t.call_id:
                deps[t.call_id].add(producer)

    def _key(cid: str) -> tuple[str, str]:
        t = by_id[cid]
        return (t.started_at or t.ended_at or "", t.call_id)

    ready = deque(sorted((cid for cid, d in deps.items() if not d), key=_key))
    ordered: list[str] = []
    remaining = dict(deps)
    while ready:
        cid = ready.popleft()
        ordered.append(cid)
        remaining.pop(cid, None)
        newly: list[str] = []
        for other, d in remaining.items():
            if cid in d:
                d.discard(cid)
                if not d:
                    newly.append(other)
        for other in sorted(newly, key=_key):
            ready.append(other)
    # Any cyclic residue is appended deterministically (never dropped, never hung).
    for cid in sorted(remaining, key=_key):
        ordered.append(cid)
    return [by_id[cid] for cid in ordered]


_PREAMBLE_DOC = '''\
"""Deterministic reproduction of an exported clio artifact lineage.

Auto-generated by clio-agent (S7 #973). Each stage translates one recorded tool
call into its plain-tool equivalent and asserts the recorded content hash. Read
the per-stage banner: DETERMINISTIC / WRITE-BYTES stages are copy-paste; RE-RUNNABLE
stages run but may not be bit-identical; AGENTIC-ONLY / GAP stages break the chain.

Run from the crate root (the directory holding this file and ``data/``):
    python reproduce.py
"""'''

_PREAMBLE_IMPORTS = """import hashlib
import os
import shutil
import sys"""

_SHA_HELPERS = '''

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_sha(path, expected, name):
    if not expected:
        print("[reproduce] %s: no recorded sha (stat-pinned) -- existence only" % name)
        return
    actual = _sha256(path)
    if actual != expected:
        raise SystemExit(
            "[reproduce] %s: sha256 MISMATCH expected=%s actual=%s" % (name, expected, actual)
        )
    print("[reproduce] %s: sha256 OK %s" % (name, expected))
'''

#: The ``reproduce.py`` preamble: anchor on the SCRIPT's own directory via
#: ``__file__`` (valid in a run script), then the sha helpers.
_PREAMBLE = (
    _PREAMBLE_DOC
    + "\n\n"
    + _PREAMBLE_IMPORTS
    + "\n\n_HERE = os.path.dirname(os.path.abspath(__file__))\nos.chdir(_HERE)\n"
    + _SHA_HELPERS
)

#: The notebook's first code cell (finding [4]): a Jupyter kernel has NO ``__file__``,
#: so the script preamble's ``os.path.abspath(__file__)`` bootstrap raises ``NameError``
#: on the very first cell and the whole notebook is dead. This variant is cwd-anchored
#: and asserts the expected crate layout up front instead — the user runs the notebook
#: FROM the crate root, and a missing ``data/`` is a clear, immediate error.
_NOTEBOOK_PREAMBLE = (
    _PREAMBLE_IMPORTS
    + "\n\n"
    + "# Jupyter kernels have no __file__: anchor on the working directory. Run this\n"
    + "# notebook FROM the crate root (the folder holding reproduce.ipynb and data/).\n"
    + 'if not os.path.isdir("data"):\n'
    + "    raise RuntimeError(\n"
    + '        "run this notebook from the crate root: expected a ./data directory next "\n'
    + '        "to reproduce.ipynb (cwd=%s); in Jupyter run os.chdir(\\"/path/to/crate\\")."\n'
    + "        % os.getcwd()\n"
    + "    )\n"
    + _SHA_HELPERS
)


def compile_reproduce(
    transforms: list[TransformRecord],
    nodes: dict[str, ArtifactNode],
    *,
    environment: Optional[EnvironmentRecord] = None,
) -> ReproduceScript:
    """Compile a ``reproduce.py`` from a lineage's transforms + resolved nodes.

    ``nodes`` maps every relay ``artifact_id`` in the lineage to its resolved
    descriptor (name / sha / custody / bundle path). ``environment`` gates the
    determinism verdict (a stage is DETERMINISTIC only when the env tier is at least
    ``lockfile-hash``); when omitted the first transform's environment is used.
    """
    ordered = _topological_order(transforms)
    # The ``environment`` param is a fallback for the header banner + an empty lineage
    # only; per-stage determinism is gated by THAT stage's OWN transform environment
    # (finding [6]), never a blanket tier taken from the first transform.
    env = environment or (ordered[0].environment if ordered else EnvironmentRecord())

    stages: list[CompiledStage] = []
    emitted_write_bytes: set[str] = set()

    def _emit_write_bytes_stage(node: ArtifactNode, index: int) -> CompiledStage:
        """A model-designated inline artifact: write the recorded bytes from CAS."""
        code = [
            # ``bundle_path`` is the full crate-relative path (``data/<ws>/<file>``,
            # workspace-namespaced — finding [11]); use it verbatim, never a basename.
            f"_src = {node.bundle_path!r}" if node.bundle_path else "_src = None",
            f"_out = {node.name!r}",
        ]
        if node.bundle_path:
            code += [
                "shutil.copyfile(_src, _out)  # model-designated inline content (from CAS)",
                f"_assert_sha(_out, {node.sha256!r}, {node.name!r})",
            ]
            verdict = StageVerdict.WRITE_BYTES
            reason = ""
        else:
            code += [
                _raise_line(
                    f"[reproduce] inline content bytes were not exported "
                    f"(over CAS threshold): {node.name}"
                )
            ]
            verdict = StageVerdict.GAP_BREAK
            reason = "inline_bytes_not_exported"
        return CompiledStage(
            index=index,
            call_id="",
            tool="(inline content)",
            verdict=verdict,
            reason=reason,
            code=code,
            output_names=[node.name],
        )

    index = 0
    for transform in ordered:
        index += 1
        inputs = [nodes[e.artifact_id] for e in transform.used if e.artifact_id in nodes]
        outputs = [nodes[e.artifact_id] for e in transform.generated if e.artifact_id in nodes]
        gap_output = next((n for n in outputs if n.mechanism == "none"), None)

        if gap_output is not None:
            verdict = StageVerdict.GAP_BREAK
            reason = "gap_version_unknown_producer"
            code = [
                _raise_line(
                    f"[reproduce] lineage gap: {gap_output.name} was produced by an "
                    "unknown agent (mechanism=none); the chain cannot be reproduced "
                    "past this point"
                )
            ]
        else:
            verdict, reason, code, assertable = _translate_stage(
                transform.instrument.tool, dict(transform.instrument.args), inputs, outputs
            )
            if not code and verdict is StageVerdict.AGENTIC_ONLY:
                code = _mcp_reference(
                    transform.instrument.tool,
                    dict(transform.instrument.args),
                    outputs,
                    reason=reason,
                )
            elif verdict is StageVerdict.DETERMINISTIC:
                # Downgrade honestly from THIS stage's OWN environment tier (finding
                # [6]) + its own input pinning — never a blanket first-transform gate.
                inputs_pinned = all(
                    e.evidence in (EdgeEvidence.HASH_PAIR,) and e.sha256
                    for e in transform.used
                    if e.artifact_id
                )
                if not tier_at_least(transform.environment.tier, EnvironmentTier.LOCKFILE_HASH):
                    verdict, reason = StageVerdict.RE_RUNNABLE, "env_below_lockfile_hash"
                elif transform.used and not inputs_pinned:
                    verdict, reason = StageVerdict.RE_RUNNABLE, "inputs_not_hash_pinned"
            # Append the sha assert ONLY for the output the translation actually writes
            # (the primary output), and only when the code writes bytes (``assertable``).
            # A secondary generated output the stage does NOT write gets a typed note,
            # never an ``_assert_sha`` against a file the code never created (findings
            # [8]/[14] — that would crash with FileNotFoundError, not a clean verdict).
            if assertable and verdict in (
                StageVerdict.DETERMINISTIC,
                StageVerdict.RE_RUNNABLE,
                StageVerdict.WRITE_BYTES,
            ):
                primary = _primary_output(outputs)
                primary_id = primary.artifact_id if primary is not None else ""
                for node in outputs:
                    if node.artifact_id == primary_id:
                        code.append(f"_assert_sha({node.name!r}, {node.sha256!r}, {node.name!r})")
                    else:
                        code.append(
                            f"# unreproduced_output: {node.name} "
                            f"(this stage's translation writes only "
                            f"{primary.name if primary is not None else '?'})"
                        )

        stages.append(
            CompiledStage(
                index=index,
                call_id=transform.call_id,
                tool=transform.instrument.tool,
                verdict=verdict,
                reason=reason,
                code=code,
                output_names=[n.name for n in outputs],
            )
        )
        for node in outputs:
            emitted_write_bytes.add(node.artifact_id)

    # Model-designated inline artifacts with no producing transform (a report the
    # model authored via create_artifact): emit write-these-bytes stages so the
    # bundle reproduces them from CAS.
    for artifact_id, node in nodes.items():
        if artifact_id in emitted_write_bytes:
            continue
        if node.mechanism == "model" or (node.bundle_path and node.mechanism != "none"):
            index += 1
            stages.append(_emit_write_bytes_stage(node, index))

    # Assemble the text.
    lines: list[str] = [_PREAMBLE, ""]
    lines.append(
        f"# Environment: clio {env.clio_version or '?'} tier={env.tier.value} "
        f"python={env.python_version or '?'} os={env.os or '?'}"
    )
    lines.append(
        f"# Stages: {len(stages)} "
        f"({sum(1 for s in stages if s.verdict in (StageVerdict.DETERMINISTIC, StageVerdict.WRITE_BYTES))} deterministic, "
        f"{sum(1 for s in stages if s.verdict is StageVerdict.RE_RUNNABLE)} re-runnable, "
        f"{sum(1 for s in stages if s.verdict is StageVerdict.AGENTIC_ONLY)} agentic-only, "
        f"{sum(1 for s in stages if s.verdict is StageVerdict.GAP_BREAK)} gap)"
    )
    lines.append("")
    for stage in stages:
        lines.append(
            f"# --- Stage {stage.index}: {stage.tool} "
            f"[{stage.verdict.value}{(': ' + stage.reason) if stage.reason else ''}] ---"
        )
        if stage.output_names:
            lines.append(f"# produces: {', '.join(stage.output_names)}")
        lines.extend(stage.code or ["pass  # nothing to reproduce for this stage"])
        lines.append("")
    lines.append('print("[reproduce] all stages complete")')
    text = "\n".join(lines) + "\n"
    return ReproduceScript(text=text, stages=stages)


def compile_notebook(script: ReproduceScript) -> dict:
    """Project a compiled script to a Jupyter notebook (one code cell per stage).

    The staged variant the owner extension asks for: a markdown header, then one
    code cell per stage with its verdict banner, so a user can run/inspect stages
    individually. The first code cell is the KERNEL-SAFE preamble (finding [4]): it
    has no ``__file__`` (undefined in Jupyter) and asserts the crate layout instead.
    """
    cells: list[dict] = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Deterministic reproduction (clio artifacts, S7 #973)\n",
                "\n",
                f"{script.deterministic_stages} deterministic, "
                f"{script.agentic_only_stages} agentic-only, {script.gap_stages} gap stages. "
                "Run cells top-to-bottom from the crate root.",
            ],
        },
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": [_NOTEBOOK_PREAMBLE],
        },
    ]
    for stage in script.stages:
        banner = (
            f"# Stage {stage.index}: {stage.tool} "
            f"[{stage.verdict.value}{(': ' + stage.reason) if stage.reason else ''}]"
        )
        source = [banner + "\n"] + [line + "\n" for line in (stage.code or ["pass"])]
        cells.append(
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source,
            }
        )
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


__all__ = [
    "ArtifactNode",
    "CompiledStage",
    "ReproduceScript",
    "StageVerdict",
    "compile_notebook",
    "compile_reproduce",
]
