#!/usr/bin/env python3
"""Run CLIO/GACT demo benchmarks against a live real-provider backend.

The runner is intentionally outside pytest: it is for long-form demo and
provider-hardening passes where every prompt, tool call, artifact, child
session, and caveat should be captured as evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_benchmark_data import create_benchmark_data


@dataclass(frozen=True)
class DemoCase:
    """One natural-language benchmark/demo prompt."""

    case_id: str
    title: str
    category: str
    prompt: str
    why: str
    expected: str
    session_group: str
    timeout_s: float = 480.0
    expected_agent: str = ""
    expected_tool_prefixes: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    expected_terms: tuple[str, ...] = ()
    min_children: int = 0
    expects_error: bool = False
    complexity_tags: tuple[str, ...] = ()
    routing_mode: str = "auto"


@dataclass
class DemoResult:
    """Recorded result for one demo case."""

    case: DemoCase
    session_id: str
    elapsed_s: float
    message: dict[str, Any]
    provider: dict[str, Any]
    child_sessions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def selected_agent(self) -> str:
        """Return selected agent from the routing part, if present."""
        return _routing_agent(self.message)

    @property
    def text(self) -> str:
        """Return visible assistant text."""
        return _message_text(self.message)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return tool call metadata."""
        return _tools(self.message)

    @property
    def tool_names(self) -> list[str]:
        """Return tool call names."""
        return [_tool_name(row) for row in self.tools]

    @property
    def artifacts(self) -> list[str]:
        """Return artifact path candidates found in tools or text."""
        return _artifact_paths(self.message)

    @property
    def blocking_error(self) -> dict[str, Any] | None:
        """Return blocking error_info, allowing documented partial recovery."""
        return _blocking_error(self.message)

    @property
    def passed(self) -> bool:
        """Return whether this case satisfied its declared expectations."""
        if self.case.expects_error:
            return self.blocking_error is not None and not self.text.strip()
        if self.blocking_error is not None:
            return False
        if self.case.expected_agent and self.selected_agent != self.case.expected_agent:
            return False
        for expected_tool in self.case.expected_tools:
            if expected_tool not in self.tool_names:
                return False
        for prefix in self.case.expected_tool_prefixes:
            if not any(name.startswith(prefix) for name in self.tool_names):
                return False
        lowered = self.text.lower()
        for term in self.case.expected_terms:
            if term.lower() not in lowered:
                return False
        if len(self.child_sessions) < self.case.min_children:
            return False
        return True

    @property
    def complexity_score(self) -> int:
        """Score cases for the best-demo report."""
        return (
            len(set(self.tool_names)) * 3
            + len(self.tools)
            + len(self.child_sessions) * 6
            + len(self.artifacts) * 4
            + len(self.case.complexity_tags) * 2
        )


def _message_text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text", "")) for part in message.get("parts", []))


def _routing_agent(message: dict[str, Any]) -> str:
    for part in message.get("parts", []):
        if part.get("type") == "routing_decision":
            return str(part.get("selected_agent", ""))
    return ""


def _routing_decision(message: dict[str, Any]) -> dict[str, Any]:
    for part in message.get("parts", []):
        if part.get("type") == "routing_decision":
            return dict(part)
    return {}


def _tools(message: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (message.get("metadata") or {}).get("tools_called") or []
    return rows if isinstance(rows, list) else []


def _tool_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("tool") or "")


def _blocking_error(message: dict[str, Any]) -> dict[str, Any] | None:
    error_info = message.get("error_info")
    if not isinstance(error_info, dict):
        return None
    details = error_info.get("details")
    if (
        isinstance(details, dict)
        and details.get("partial") is True
        and details.get("stage")
        in {
            "post_observation_planning",
            "parallel_validation_recovery",
            "step_limit_after_observations",
        }
    ):
        return None
    return error_info


def _artifact_paths(message: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for row in _tools(message):
        result = row.get("result")
        if isinstance(result, str):
            candidates.extend(re.findall(r"[A-Za-z]:\\[^\n\r]+?\.png|/[^\s]+?\.png", result))
        elif isinstance(result, dict):
            for value in result.values():
                if isinstance(value, str) and value.endswith(".png"):
                    candidates.append(value)
    candidates.extend(
        re.findall(r"[A-Za-z]:\\[^\n\r]+?\.png|/[^\s]+?\.png", _message_text(message))
    )
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _children(http: httpx.Client, parent_session_id: str) -> list[dict[str, Any]]:
    sessions = http.get("/v1/sessions").json()["sessions"]
    return [row for row in sessions if row.get("parent_session_id") == parent_session_id]


def _post_turn(
    http: httpx.Client,
    session_id: str,
    prompt: str,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    ack = http.post(
        f"/v1/sessions/{session_id}/messages",
        json={"parts": [{"type": "text", "text": prompt}]},
    )
    ack.raise_for_status()
    user_id = ack.json()["message_id"]

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        messages = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        for index, message in enumerate(messages):
            if message.get("id") == user_id:
                if index > 0 and messages[index - 1].get("role") == "assistant":
                    assistant = messages[index - 1]
                    stop_reason = str(assistant.get("stop_reason") or "")
                    if stop_reason or assistant.get("error_info") is not None:
                        return assistant
                break
        time.sleep(0.5)
    raise TimeoutError(f"assistant turn for {user_id!r} did not settle in {timeout_s:g}s")


def _provider(http: httpx.Client) -> dict[str, Any]:
    try:
        return http.get("/v1/providers/lm").json()
    except Exception as exc:
        return {"error": str(exc)}


def _case_row(result: DemoResult) -> dict[str, Any]:
    return {
        "case": result.case.case_id,
        "title": result.case.title,
        "category": result.case.category,
        "prompt": result.case.prompt,
        "expected": result.case.expected,
        "why": result.case.why,
        "session_id": result.session_id,
        "elapsed_s": round(result.elapsed_s, 3),
        "passed": result.passed,
        "selected_agent": result.selected_agent,
        "routing_decision": _routing_decision(result.message),
        "tools_called": result.tools,
        "tool_names": result.tool_names,
        "child_sessions": result.child_sessions,
        "artifacts": result.artifacts,
        "error_info": result.message.get("error_info"),
        "stop_reason": result.message.get("stop_reason"),
        "provider": result.provider,
        "routing_mode": result.case.routing_mode,
        "complexity_score": result.complexity_score,
        "answer_excerpt": result.text[:1200],
        "complexity_tags": list(result.case.complexity_tags),
    }


def _make_cases(manifest: dict[str, Any]) -> list[DemoCase]:
    h5 = manifest["hdf5"]["path"]
    parquet = manifest["parquet"]["path"]
    dirty = manifest["parquet"]["dirty_path"]
    csv_path = manifest["csv"]["path"]
    adios = manifest["adios"]["path"]
    missing = str(Path(h5).with_name("missing_fusion_run.h5"))

    return [
        DemoCase(
            case_id="workflow_hdf5_overview",
            title="HDF5 fusion file overview",
            category="tooling",
            session_group="workflow",
            expected_agent="data",
            expected_tool_prefixes=("hdf5_",),
            expected_terms=("electron_temperature", "density", "heat_flux"),
            complexity_tags=("hdf5", "data-expert", "tool-result-synthesis"),
            prompt=(
                f"I need to brief collaborators on this fusion output: {h5}. "
                "What datasets are inside, what shapes and units matter, and what "
                "compression details should I mention?"
            ),
            expected="Data expert uses HDF5 tools and summarizes datasets, units, and compression.",
            why="Proves real HDF5 path handling, tool argument generation, and grounded synthesis.",
        ),
        DemoCase(
            case_id="workflow_parquet_profile",
            title="Parquet facility profile",
            category="analysis",
            session_group="workflow",
            expected_agent="analysis",
            expected_tools=("parquet_analyze_schema",),
            expected_tool_prefixes=("parquet_",),
            expected_terms=("temperature_k", "pressure_pa", "anomaly_score"),
            complexity_tags=("parquet", "statistics", "analysis-expert"),
            prompt=(
                f"Profile the facility measurements in {parquet}. I care about schema, row groups, "
                "and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane."
            ),
            expected="Analysis expert reads Parquet schema and computes statistics for named fields.",
            why="Checks statistical tool calls and model feedback from multiple numeric observations.",
        ),
        DemoCase(
            case_id="workflow_memory_followup",
            title="Memory follow-up without repeating path",
            category="memory",
            session_group="workflow",
            expected_agent="analysis",
            expected_tool_prefixes=("parquet_",),
            expected_terms=("anomaly", "temperature", "pressure"),
            complexity_tags=("memory", "session-context", "analysis-expert"),
            prompt=(
                "Based on the Parquet file we just profiled, compute whatever schema or column "
                "statistics you need for a quick anomaly triage view. Do not ask me for the path "
                "again."
            ),
            expected="CLIO resolves the previously profiled Parquet file from session context.",
            why="Demonstrates session memory and current-file resolution instead of copy/paste paths.",
        ),
        DemoCase(
            case_id="workflow_csv_event_schema",
            title="CSV event stream schema",
            category="analysis",
            session_group="workflow",
            expected_agent="analysis",
            expected_tools=("csv_read_table",),
            expected_terms=("event_id", "status", "operator_note"),
            complexity_tags=("csv", "analysis-expert", "tool-scope"),
            prompt=(
                f"This event stream came with the run: {csv_path}. What columns does it contain, "
                "and where are the status and operator_note fields?"
            ),
            expected="Analysis expert uses csv_read_table, not shell shortcuts.",
            why="Regression coverage for scoped utility tools and native CSV inspection.",
        ),
        DemoCase(
            case_id="workflow_visual_dashboard",
            title="Follow-up visualization artifact",
            category="visualization",
            session_group="workflow",
            expected_agent="visualization",
            expected_tools=("plot_summary",),
            expected_terms=(".png", "dashboard"),
            complexity_tags=("visualization", "artifact", "multi-turn"),
            prompt=(
                "Create a compact PNG dashboard from the Parquet file we just profiled. "
                "Tell me where it was saved and what the chart is summarizing."
            ),
            expected="Visualization expert resolves prior Parquet context and creates a PNG artifact.",
            why="Shows multi-turn handoff from analysis to visualization with a real saved artifact.",
        ),
        DemoCase(
            case_id="hdf5_dataset_focus",
            title="Natural HDF5 dataset deep dive",
            category="tooling",
            session_group="hdf5_dataset",
            expected_agent="data",
            expected_tools=("hdf5_analyze_dataset",),
            expected_terms=("plasma/electron_temperature", "shape", "chunk"),
            complexity_tags=("hdf5", "dataset-level", "natural-routing"),
            prompt=(
                f"Focus on plasma/electron_temperature inside {h5}. What shape, chunks, "
                "compression, and statistics matter if we mostly read it over time?"
            ),
            expected="Data expert recognizes the named dataset and calls hdf5_analyze_dataset.",
            why="Catches whether natural dataset references require tool-shaped user wording.",
        ),
        DemoCase(
            case_id="cross_file_triage_nanoagents",
            title="Cross-file triage with tier-3 workers",
            category="multi-agent",
            session_group="cross_file",
            expected_agent="analysis",
            expected_terms=("data_validator", "analysis_validator", "csv_validator"),
            min_children=3,
            complexity_tags=("nanoagents", "tier-3", "hdf5", "parquet", "csv", "adios"),
            prompt=(
                f"I have four related files from the same experiment: {h5}, {parquet}, "
                f'{csv_path}, and "{adios}". Give me a cross-file triage summary: what is '
                "in each file, whether the measurements look ready for downstream analysis, "
                "and what I should check next."
            ),
            expected="Analysis coordinates tool-backed child workers and aggregates their findings.",
            why="Best stress case for hierarchical routing and child-session evidence.",
        ),
        DemoCase(
            case_id="reasoning_cross_file_triage_nanoagents",
            title="No-guard cross-file triage",
            category="planner-hardening",
            session_group="reasoning_cross_file",
            routing_mode="reasoning_only",
            expected_agent="analysis",
            expected_terms=("data_validator", "analysis_validator", "csv_validator"),
            min_children=3,
            timeout_s=720.0,
            complexity_tags=("no-guard", "planner", "nanoagents", "tier-3", "multi-file"),
            prompt=(
                f"I have four related files from the same experiment: {h5}, {parquet}, "
                f'{csv_path}, and "{adios}". Give me a cross-file triage summary: what is '
                "in each file, whether the measurements look ready for downstream analysis, "
                "and what I should check next."
            ),
            expected=(
                "With routing guards disabled for the session, the planner still reaches "
                "analysis and tool-backed child workers."
            ),
            why=(
                "Separates planner capability from the production registry guard, which matters "
                "as CLIO grows beyond a few built-in experts."
            ),
        ),
        DemoCase(
            case_id="adios_bp5_container",
            title="ADIOS/BP5 container inspection",
            category="tooling",
            session_group="adios",
            expected_agent="data",
            expected_tools=("adios_inspect_file",),
            expected_terms=("BP5", "profiling"),
            complexity_tags=("adios", "bp5", "hpc-format"),
            prompt=(
                f'This ADIOS BP5 output came from a Gray-Scott run: "{adios}". Tell me what '
                "the container looks like, whether profiling metadata is present, and what "
                "extra runtime is needed if variable-level metadata is unavailable."
            ),
            expected="Data expert inspects BP5 container/profiling and surfaces ADIOS2 caveats.",
            why="Exercises HPC container handling and honest dependency limitations.",
        ),
        DemoCase(
            case_id="reasoning_adios_bp5_container",
            title="No-guard ADIOS/BP5 route",
            category="planner-hardening",
            session_group="reasoning_adios",
            routing_mode="reasoning_only",
            expected_agent="data",
            expected_tools=("adios_inspect_file",),
            expected_terms=("BP5", "profiling"),
            timeout_s=620.0,
            complexity_tags=("no-guard", "planner", "adios", "bp5"),
            prompt=(
                f'This ADIOS BP5 output came from a Gray-Scott run: "{adios}". Tell me what '
                "the container looks like, whether profiling metadata is present, and what "
                "extra runtime is needed if variable-level metadata is unavailable."
            ),
            expected="With routing guards disabled, the planner still selects the data expert.",
            why="Checks that BP5 routing is not only a hardcoded suffix guard behavior.",
        ),
        DemoCase(
            case_id="dirty_parquet_quality",
            title="Dirty Parquet quality review",
            category="analysis",
            session_group="dirty",
            expected_agent="analysis",
            expected_tool_prefixes=("parquet_",),
            expected_terms=("temperature_k", "pressure_pa", "quality_flag"),
            complexity_tags=("dirty-data", "quality", "statistics"),
            prompt=(
                f"This Parquet export looks suspicious: {dirty}. Review it for data quality "
                "problems and tell me what fields need attention before downstream analysis."
            ),
            expected="Analysis expert uses Parquet tools and grounds quality claims in columns/nulls.",
            why="Separates concrete data-quality findings from generic cleaning advice.",
        ),
        DemoCase(
            case_id="ndp_catalog_discovery",
            title="NDP catalog discovery",
            category="external-catalog",
            session_group="ndp",
            expected_agent="analysis",
            expected_tool_prefixes=("ndp_",),
            expected_terms=("dataset",),
            timeout_s=620.0,
            complexity_tags=("ndp", "clio-kit", "external-mcp"),
            prompt=(
                "Find a few NOAA or climate-related datasets in the National Data Platform "
                "catalog that might complement this facility data. Summarize what you found "
                "and what I should verify before download."
            ),
            expected="Analysis expert calls NDP tools through the CLIO gateway.",
            why="Exercises external catalog tools and tool-result feedback beyond local files.",
        ),
        DemoCase(
            case_id="visual_scatter_artifact",
            title="Targeted scatter plot",
            category="visualization",
            session_group="scatter",
            expected_agent="visualization",
            expected_tools=("plot_scatter",),
            expected_terms=(".png", "anomaly_score", "vibration"),
            timeout_s=620.0,
            complexity_tags=("visualization", "specific-tool", "artifact"),
            prompt=(
                f"Create a scatter plot from {parquet} with vibration_mm_s on the x-axis and "
                "anomaly_score on the y-axis. Save it as a PNG and explain what relationship "
                "the plot is meant to reveal."
            ),
            expected="Visualization expert chooses plot_scatter and saves a PNG artifact.",
            why="Checks whether a specific visualization intent maps to the right chart tool.",
        ),
        DemoCase(
            case_id="missing_hdf5_error",
            title="Missing file error surfacing",
            category="hardening",
            session_group="errors",
            expected_agent="data",
            expected_tool_prefixes=("hdf5_",),
            expects_error=True,
            complexity_tags=("error-surfacing", "no-fake-answer"),
            prompt=(
                f"Inspect this HDF5 file and tell me what datasets are inside: {missing}. "
                "If the file is unavailable, surface the real error."
            ),
            expected="CLIO returns structured error_info and no normal fake assistant answer.",
            why="Verifies errors are surfaced, not hidden behind canned or repeated text.",
        ),
    ]


def _session_key(case: DemoCase) -> str:
    """Return stable session bucket for a case."""
    return f"{case.session_group}:{case.routing_mode}"


def _create_sessions(http: httpx.Client, cases: list[DemoCase]) -> dict[str, str]:
    session_ids: dict[str, str] = {}
    for key in dict.fromkeys(_session_key(case) for case in cases):
        group, routing_mode = key.rsplit(":", 1)
        payload = {"title": f"demo {group}"}
        if routing_mode != "auto":
            payload["routing_mode"] = routing_mode
        response = http.post("/v1/sessions", json=payload)
        response.raise_for_status()
        session_ids[key] = response.json()["id"]
    return session_ids


def run_benchmark(base_url: str, data_dir: Path, output_jsonl: Path, report_path: Path) -> int:
    """Run demo cases and write JSONL plus a markdown report."""
    manifest = create_benchmark_data(data_dir)
    cases = _make_cases(manifest)
    results: list[DemoResult] = []
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=base_url, timeout=90.0) as http:
        health = http.get("/v1/health")
        health.raise_for_status()
        session_ids = _create_sessions(http, cases)
        provider = _provider(http)

        with output_jsonl.open("w", encoding="utf-8") as log:
            for index, case in enumerate(cases, start=1):
                session_id = session_ids[_session_key(case)]
                before_children = {child["id"] for child in _children(http, session_id)}
                print(f"[{index}/{len(cases)}] {case.case_id}: {case.title}", flush=True)
                started = time.monotonic()
                message = _post_turn(http, session_id, case.prompt, timeout_s=case.timeout_s)
                elapsed_s = time.monotonic() - started
                after_children = _children(http, session_id)
                new_children = [
                    child for child in after_children if child.get("id") not in before_children
                ]
                result = DemoResult(
                    case=case,
                    session_id=session_id,
                    elapsed_s=elapsed_s,
                    message=message,
                    provider=provider,
                    child_sessions=new_children,
                )
                results.append(result)
                log.write(json.dumps(_case_row(result), ensure_ascii=False, default=str) + "\n")
                log.flush()
                status = "PASS" if result.passed else "FAIL"
                print(
                    f"  {status} agent={result.selected_agent or '-'} "
                    f"tools={','.join(result.tool_names) or '-'} "
                    f"children={len(result.child_sessions)} elapsed={elapsed_s:.1f}s",
                    flush=True,
                )

    report_path.write_text(_render_report(results, output_jsonl), encoding="utf-8")
    return 0 if all(result.passed for result in results) else 1


def _render_report(results: list[DemoResult], output_jsonl: Path) -> str:
    passed = sum(1 for result in results if result.passed)
    best = sorted(results, key=lambda result: result.complexity_score, reverse=True)[:10]
    lines = [
        "# CLIO ALCF Demo Benchmark Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Evidence JSONL: `{output_jsonl}`",
        "",
        f"Result: {passed}/{len(results)} cases passed.",
        "",
        "## All Cases",
        "",
        "| Case | Category | Mode | Source | Pass | Agent | Tools | Children | Elapsed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        route_source = (_routing_decision(result.message).get("metadata") or {}).get(
            "route_source", "-"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    result.case.case_id,
                    result.case.category,
                    result.case.routing_mode,
                    str(route_source),
                    "yes" if result.passed else "no",
                    result.selected_agent or "-",
                    ", ".join(result.tool_names) or "-",
                    str(len(result.child_sessions)),
                    f"{result.elapsed_s:.1f}s",
                ]
            )
            + " |"
        )

    lines.extend(["", "## Best 10 Demo Prompts", ""])
    for rank, result in enumerate(best, start=1):
        tool_text = ", ".join(result.tool_names) or "none"
        artifact_text = ", ".join(result.artifacts) or "none"
        child_text = ", ".join(
            child.get("title", child.get("id", "")) for child in result.child_sessions
        )
        lines.extend(
            [
                f"### {rank}. {result.case.title}",
                "",
                f"Case: `{result.case.case_id}`",
                f"Category: {result.case.category}",
                f"Routing mode: `{result.case.routing_mode}`",
                f"Status: {'pass' if result.passed else 'fail'}",
                f"Selected agent: `{result.selected_agent or '-'}`",
                f"Tools: {tool_text}",
                f"Child sessions: {child_text or 'none'}",
                f"Artifacts: {artifact_text}",
                f"Elapsed: {result.elapsed_s:.1f}s",
                "",
                "Prompt:",
                "",
                "```text",
                result.case.prompt,
                "```",
                "",
                f"What to see: {result.case.expected}",
                "",
                f"Why this is interesting: {result.case.why}",
                "",
                "Observed excerpt:",
                "",
                "```text",
                result.text[:900].strip() or "<no assistant text>",
                "```",
                "",
            ]
        )

    failures = [result for result in results if not result.passed]
    if failures:
        lines.extend(["## Failures To Investigate", ""])
        for result in failures:
            lines.extend(
                [
                    f"- `{result.case.case_id}`: expected {result.case.expected}",
                    f"  observed agent={result.selected_agent or '-'}, "
                    f"tools={', '.join(result.tool_names) or '-'}, "
                    f"error={result.message.get('error_info')}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:17960",
        help="Live clio-agent-gact base URL.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("tmp/clio-benchmark-data"),
        help="Benchmark data directory.",
    )
    parser.add_argument(
        "--output-jsonl",
        type=Path,
        default=Path("tmp/clio-demo-benchmark-alcf.jsonl"),
        help="Output evidence JSONL path.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/ALCF_DEMO_BENCHMARK_REPORT.md"),
        help="Output markdown report path.",
    )
    args = parser.parse_args()
    raise SystemExit(
        run_benchmark(
            args.base_url.rstrip("/"),
            args.data_dir.resolve(),
            args.output_jsonl.resolve(),
            args.report.resolve(),
        )
    )


if __name__ == "__main__":
    main()
