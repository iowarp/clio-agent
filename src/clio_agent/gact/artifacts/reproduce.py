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


def _translate_stage(
    tool: str, args: dict, inputs: list[ArtifactNode], outputs: list[ArtifactNode]
) -> tuple[StageVerdict, str, list[str]]:
    """Translate a tool call to code + a base verdict (before pin/env gating).

    Returns ``(verdict, reason, code_lines)``. The verdict here reflects only
    *translatability*; :func:`compile_reproduce` downgrades a DETERMINISTIC stage to
    RE_RUNNABLE when an input is not content-pinned or the env tier is too weak.
    """
    lname = tool.lower()
    out = _primary_output(outputs)
    out_path = out.name if out is not None else "output.bin"

    # ---- staged download (stage_resource / fetch / download) ----------------
    if any(tok in lname for tok in ("stage_resource", "stage", "download", "fetch")):
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
            return StageVerdict.RE_RUNNABLE, "staged_download_authority_input", code
        return StageVerdict.AGENTIC_ONLY, "stage_missing_source_url", []

    # ---- pandas filter (pandas_filter_data / filter / query) ----------------
    if any(tok in lname for tok in ("pandas", "filter_data", "filter", "query")):
        in_path = inputs[0].name if inputs else _pick(args, "input_path", "path", "csv")
        expr = _pick(args, "expression", "query", "filter", "pandas_expression", "where")
        if in_path:
            code = [
                "import pandas as pd",
                f"_df = pd.read_csv({in_path!r})",
            ]
            if expr:
                code.append(f"_df = _df.query({expr!r})  # recorded filter expression")
            columns = args.get("columns")
            if isinstance(columns, list) and columns:
                code.append(f"_df = _df[{list(columns)!r}]  # recorded column projection")
            code.append(f"_df.to_csv({out_path!r}, index=False)")
            return StageVerdict.DETERMINISTIC, "", code
        return StageVerdict.AGENTIC_ONLY, "filter_missing_input", []

    # ---- timeseries plot (plot_timeseries / plot / chart / figure) ----------
    if any(tok in lname for tok in ("plot_timeseries", "plot", "chart", "figure", "timeseries")):
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
            return StageVerdict.RE_RUNNABLE, "raster_render_env_sensitive", code
        return StageVerdict.AGENTIC_ONLY, "plot_missing_input", []

    # ---- unknown tool -> agentic-only reference -----------------------------
    return StageVerdict.AGENTIC_ONLY, "no_registry_translation", []


def _agentic_reference(
    tool: str, args: dict, outputs: list[ArtifactNode], reason: str
) -> list[str]:
    """The non-executable MCP-invocation reference for an untranslatable stage."""
    out = _primary_output(outputs)
    out_name = out.name if out is not None else "(no output)"
    try:
        args_repr = json.dumps(args, default=str, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        args_repr = repr(args)
    lines = [
        f"# AGENTIC-ONLY ({reason}): no registry translation for tool {tool!r}.",
        "# Re-hand the recorded task to a live agent, or invoke the MCP tool directly:",
        f"#   tool: {tool}",
    ]
    lines.extend(f"#   {line}" for line in f"args: {args_repr}".splitlines())
    lines.append(f"#   -> produces: {out_name}")
    lines.append(
        _raise_line(
            f"[reproduce] agentic-only stage: cannot reproduce {out_name} "
            f"deterministically (tool {tool} has no translation)"
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


_PREAMBLE = '''\
"""Deterministic reproduction of an exported clio artifact lineage.

Auto-generated by clio-agent (S7 #973). Each stage translates one recorded tool
call into its plain-tool equivalent and asserts the recorded content hash. Read
the per-stage banner: DETERMINISTIC / WRITE-BYTES stages are copy-paste; RE-RUNNABLE
stages run but may not be bit-identical; AGENTIC-ONLY / GAP stages break the chain.

Run from the crate root (the directory holding this file and ``data/``):
    python reproduce.py
"""

import hashlib
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)


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
    env = environment or (ordered[0].environment if ordered else EnvironmentRecord())
    env_ok = tier_at_least(env.tier, EnvironmentTier.LOCKFILE_HASH)

    stages: list[CompiledStage] = []
    emitted_write_bytes: set[str] = set()

    def _emit_write_bytes_stage(node: ArtifactNode, index: int) -> CompiledStage:
        """A model-designated inline artifact: write the recorded bytes from CAS."""
        code = [
            f"_src = os.path.join('data', {node.bundle_path.split('/')[-1]!r})"
            if node.bundle_path
            else "_src = None",
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
            verdict, reason, code = _translate_stage(
                transform.instrument.tool, dict(transform.instrument.args), inputs, outputs
            )
            if not code and verdict is StageVerdict.AGENTIC_ONLY:
                code = _agentic_reference(
                    transform.instrument.tool, dict(transform.instrument.args), outputs, reason
                )
            elif verdict is StageVerdict.DETERMINISTIC:
                # Downgrade honestly: an unpinned/authority input or a weak env tier
                # means the run is described but not bit-identical-guaranteed.
                inputs_pinned = all(
                    e.evidence in (EdgeEvidence.HASH_PAIR,) and e.sha256
                    for e in transform.used
                    if e.artifact_id
                )
                if not env_ok:
                    verdict, reason = StageVerdict.RE_RUNNABLE, "env_below_lockfile_hash"
                elif transform.used and not inputs_pinned:
                    verdict, reason = StageVerdict.RE_RUNNABLE, "inputs_not_hash_pinned"
            # Append the sha assert for every executable (non-agentic, non-gap) stage.
            if verdict in (
                StageVerdict.DETERMINISTIC,
                StageVerdict.RE_RUNNABLE,
                StageVerdict.WRITE_BYTES,
            ):
                for node in outputs:
                    code.append(f"_assert_sha({node.name!r}, {node.sha256!r}, {node.name!r})")

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
    individually. The preamble (hash helpers) is its own first code cell.
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
            "source": [_PREAMBLE],
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
