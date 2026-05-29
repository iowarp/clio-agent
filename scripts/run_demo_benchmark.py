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

_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES = ("guard", "user_agent_keyword", "recovery")
_DATA_FILE_SUFFIXES = {
    ".bp",
    ".bp4",
    ".bp5",
    ".cif",
    ".csv",
    ".fa",
    ".fasta",
    ".fna",
    ".geojson",
    ".gz",
    ".h5",
    ".hdf5",
    ".mzml",
    ".parquet",
    ".png",
    ".sac",
    ".tar",
    ".tgz",
    ".vcf",
}


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
    expected_agent: str | tuple[str, ...] = ""
    expected_tool_prefixes: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    expected_handoff_agents: tuple[str, ...] = ()
    expected_terms: tuple[str, ...] = ()
    expected_tool_prefix_groups: tuple[tuple[str, ...], ...] = ()
    expected_handoff_agent_groups: tuple[tuple[str, ...], ...] = ()
    expected_term_groups: tuple[tuple[str, ...], ...] = ()
    min_children: int = 0
    min_tool_calls: int = 0
    min_handoff_events: int = 0
    expects_error: bool = False
    complexity_tags: tuple[str, ...] = ()
    routing_mode: str = "auto"
    setup_prompts: tuple[str, ...] = ()
    compact_before_prompt: bool = False
    provider_swap_preset_id: str = ""
    provider_swap_model: str = ""
    expected_actions: tuple[str, ...] = ()
    cancel_after_s: float = 0.0
    expects_cancelled: bool = False
    turn_agent_id: str = ""
    forbidden_route_sources: tuple[str, ...] = ()
    min_artifacts: int = 0


@dataclass
class DemoResult:
    """Recorded result for one demo case."""

    case: DemoCase
    session_id: str
    elapsed_s: float
    message: dict[str, Any]
    provider: dict[str, Any]
    child_sessions: list[dict[str, Any]] = field(default_factory=list)
    setup_messages: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    benchmark_lane: str = "default"

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
    def expert_handoffs(self) -> list[dict[str, Any]]:
        """Return expert handoff provenance metadata."""
        return _expert_handoffs(self.message)

    @property
    def handoff_agent_ids(self) -> list[str]:
        """Return unique expert IDs observed in handoff provenance."""
        seen: set[str] = set()
        ids: list[str] = []
        for row in self.expert_handoffs:
            agent_id = str(row.get("agent_id") or "")
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            ids.append(agent_id)
        return ids

    @property
    def handoff_event_count(self) -> int:
        """Return the number of recorded expert handoff events."""
        return len(self.expert_handoffs)

    @property
    def visible_event_count(self) -> int:
        """Return visible evidence events useful for stress scoring."""
        return len(self.tools) + len(self.expert_handoffs) + len(self.child_sessions)

    @property
    def blocking_error(self) -> dict[str, Any] | None:
        """Return error_info that should fail or satisfy error-focused cases."""
        return _blocking_error(self.message)

    @property
    def partial_error(self) -> dict[str, Any] | None:
        """Return surfaced partial-recovery metadata, if this turn had any."""
        return _partial_error(self.message)

    @property
    def stream_source(self) -> str:
        """Return the recorded GACT stream source for the assistant message."""
        return str((self.message.get("metadata") or {}).get("stream_source") or "")

    @property
    def stream_fallback(self) -> dict[str, Any]:
        """Return stream fallback metadata, if present."""
        row = (self.message.get("metadata") or {}).get("stream_fallback") or {}
        return row if isinstance(row, dict) else {}

    @property
    def route_source(self) -> str:
        """Return the recorded routing source, if present."""
        metadata = _routing_decision(self.message).get("metadata") or {}
        if not isinstance(metadata, dict):
            return ""
        return str(metadata.get("route_source") or "")

    @property
    def passed(self) -> bool:
        """Return whether this case satisfied its declared expectations."""
        if self.case.expects_cancelled:
            error_info = self.message.get("error_info")
            return (
                isinstance(error_info, dict)
                and error_info.get("error") == "cancelled"
                and not self.text.strip()
            )
        if self.case.expects_error:
            return self.blocking_error is not None and not _non_telemetry_text(self.text).strip()
        if self.partial_error is not None:
            return False
        if self.blocking_error is not None:
            return False
        if self.route_source in self.case.forbidden_route_sources:
            return False
        if self.case.expected_agent:
            expected_agents = (
                (self.case.expected_agent,)
                if isinstance(self.case.expected_agent, str)
                else self.case.expected_agent
            )
            if self.selected_agent not in expected_agents:
                return False
        for expected_tool in self.case.expected_tools:
            if expected_tool not in self.tool_names:
                return False
        if not self._matches_any_group(
            tuple(self.handoff_agent_ids),
            self.case.expected_handoff_agents,
            self.case.expected_handoff_agent_groups,
        ):
            return False
        if self.case.min_tool_calls and len(self.tools) < self.case.min_tool_calls:
            return False
        if self.case.min_handoff_events and self.handoff_event_count < self.case.min_handoff_events:
            return False
        if not self._matches_any_prefix_group(
            tuple(self.tool_names),
            self.case.expected_tool_prefixes,
            self.case.expected_tool_prefix_groups,
        ):
            return False
        lowered = "\n".join([self.text, *self.artifacts]).lower()
        if not self._matches_any_text_group(
            lowered,
            self.case.expected_terms,
            self.case.expected_term_groups,
        ):
            return False
        if self.case.min_artifacts:
            verified_artifacts = [
                row
                for row in self.artifact_evidence
                if row.get("exists") and int(row.get("size_bytes") or 0) > 0
            ]
            if len(verified_artifacts) < self.case.min_artifacts:
                return False
        if len(self.child_sessions) < self.case.min_children:
            return False
        for expected_action in self.case.expected_actions:
            if not any(
                action.get("type") == expected_action and action.get("ok") is True
                for action in self.actions
            ):
                return False
        return True

    @staticmethod
    def _matches_any_group(
        values: tuple[str, ...],
        required: tuple[str, ...],
        alternatives: tuple[tuple[str, ...], ...],
    ) -> bool:
        """Return whether all required values from any acceptable group are present."""
        groups = alternatives or ((required,) if required else ())
        return not groups or any(all(item in values for item in group) for group in groups)

    @staticmethod
    def _matches_any_prefix_group(
        values: tuple[str, ...],
        required: tuple[str, ...],
        alternatives: tuple[tuple[str, ...], ...],
    ) -> bool:
        """Return whether all required prefixes from any acceptable group are present."""
        groups = alternatives or ((required,) if required else ())
        return not groups or any(
            all(any(value.startswith(prefix) for value in values) for prefix in group)
            for group in groups
        )

    @staticmethod
    def _matches_any_text_group(
        lowered_text: str,
        required: tuple[str, ...],
        alternatives: tuple[tuple[str, ...], ...],
    ) -> bool:
        """Return whether all required terms from any acceptable group are present."""
        groups = alternatives or ((required,) if required else ())
        return not groups or any(
            all(term.lower() in lowered_text for term in group) for group in groups
        )

    @property
    def outcome(self) -> str:
        """Return a human-readable outcome category."""
        if self.case.expects_cancelled:
            return "cancelled" if self.passed else "fail"
        if self.case.expects_error:
            return "expected_error" if self.passed else "fail"
        if self.partial_error is not None:
            return "partial"
        return "pass" if self.passed else "fail"

    @property
    def complexity_score(self) -> int:
        """Score cases for the best-demo report."""
        return (
            len(set(self.tool_names)) * 3
            + len(self.tools)
            + len(self.expert_handoffs) * 4
            + len(self.child_sessions) * 6
            + len(self.artifacts) * 4
            + len(self.setup_messages) * 4
            + len(self.actions) * 5
            + len(self.case.complexity_tags) * 2
        )

    @property
    def data_files(self) -> list[str]:
        """Return local data paths referenced by the prompt or tool arguments."""
        return _data_file_paths(self.case.prompt, self.tools)

    @property
    def artifact_evidence(self) -> list[dict[str, Any]]:
        """Return artifact path existence and size evidence."""
        return _artifact_evidence(self.artifacts)

    @property
    def route_graph(self) -> dict[str, Any]:
        """Return structured routing evidence for this result."""
        return _route_graph(self)

    @property
    def route_metrics(self) -> dict[str, Any]:
        """Return route depth/fanout/tool metrics used by the report."""
        return _route_metrics(self)


def _message_text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text", "")) for part in message.get("parts", []))


def _non_telemetry_text(text: str) -> str:
    """Drop CLIO's structured handoff telemetry lines from visible answer text."""

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(
            r"[A-Za-z0-9_.:-]+\s+\|\s+(success|failure)\s+\|\s+[A-Za-z0-9_.:-]+",
            stripped,
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


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


def _expert_handoffs(message: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (message.get("metadata") or {}).get("expert_handoffs") or []
    return rows if isinstance(rows, list) else []


def _tool_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("tool") or "")


def _blocking_error(message: dict[str, Any]) -> dict[str, Any] | None:
    error_info = message.get("error_info")
    if not isinstance(error_info, dict):
        return None
    if _partial_error(message) is not None:
        return None
    return error_info


def _partial_error(message: dict[str, Any]) -> dict[str, Any] | None:
    error_info = message.get("error_info")
    if not isinstance(error_info, dict):
        return None
    details = error_info.get("details")
    if not isinstance(details, dict) or details.get("partial") is not True:
        return None
    if details.get("stage") not in {
        "post_observation_planning",
        "parallel_validation_recovery",
        "step_limit_after_observations",
    }:
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


def _path_like_strings(value: Any) -> list[str]:
    """Extract path-like strings from nested tool metadata."""
    if isinstance(value, str):
        return re.findall(
            r"[A-Za-z]:\\[^\n\r\"']+|/(?:[^\s,\"'`]|\\ )+",
            value,
        )
    if isinstance(value, dict):
        paths: list[str] = []
        for item in value.values():
            paths.extend(_path_like_strings(item))
        return paths
    if isinstance(value, list):
        paths = []
        for item in value:
            paths.extend(_path_like_strings(item))
        return paths
    return []


def _clean_path_candidate(path: str) -> str:
    """Trim punctuation commonly attached to prose path references."""
    return path.strip().strip(".,;:)\"'")


def _data_file_paths(prompt: str, tools: list[dict[str, Any]]) -> list[str]:
    """Extract local data/input paths from a prompt and tool argument metadata."""
    candidates = [_clean_path_candidate(path) for path in _path_like_strings(prompt)]
    for row in tools:
        args = row.get("args") or row.get("arguments") or {}
        candidates.extend(_clean_path_candidate(path) for path in _path_like_strings(args))
    deduped: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        if Path(candidate).suffix.lower() not in _DATA_FILE_SUFFIXES:
            continue
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _artifact_evidence(artifacts: list[str]) -> list[dict[str, Any]]:
    """Return existence evidence for local artifact paths."""
    rows: list[dict[str, Any]] = []
    for raw_path in artifacts:
        path = _clean_path_candidate(raw_path)
        exists = Path(path).exists()
        size_bytes = Path(path).stat().st_size if exists and Path(path).is_file() else 0
        rows.append({"path": path, "exists": exists, "size_bytes": size_bytes})
    return rows


def _route_graph(result: DemoResult) -> dict[str, Any]:
    """Build a machine-readable route graph from routing and handoff evidence."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def add_node(node_id: str, node_type: str) -> None:
        if not node_id:
            return
        if any(row["id"] == node_id and row["type"] == node_type for row in nodes):
            return
        nodes.append({"id": node_id, "type": node_type})

    selected = result.selected_agent
    add_node("orchestrator", "orchestrator")
    if selected:
        add_node(selected, "expert")
        edges.append({"from": "orchestrator", "to": selected, "kind": "route"})

    previous = selected
    for row in result.expert_handoffs:
        agent_id = str(row.get("agent_id") or "")
        if not agent_id:
            continue
        add_node(agent_id, "expert")
        if previous and previous != agent_id:
            edges.append({"from": previous, "to": agent_id, "kind": "handoff"})
        previous = agent_id

    for child in result.child_sessions:
        child_id = str(child.get("id") or child.get("title") or child.get("name") or "")
        if not child_id:
            continue
        add_node(child_id, "child_session")
        edges.append({"from": selected or "orchestrator", "to": child_id, "kind": "branch"})

    return {"nodes": nodes, "edges": edges}


def _route_metrics(result: DemoResult) -> dict[str, Any]:
    """Return aggregate routing/tool metrics for benchmark comparison."""
    graph = _route_graph(result)
    expert_nodes = [row for row in graph["nodes"] if row.get("type") == "expert"]
    branch_edges = [row for row in graph["edges"] if row.get("kind") == "branch"]
    return {
        "expert_depth": len(expert_nodes),
        "branch_count": len(branch_edges),
        "unique_experts": len({row.get("id") for row in expert_nodes}),
        "unique_tools": len(set(result.tool_names)),
        "tool_call_count": len(result.tools),
        "artifact_count": len(result.artifacts),
    }


def _children(http: httpx.Client, parent_session_id: str) -> list[dict[str, Any]]:
    sessions = http.get("/v1/sessions").json()["sessions"]
    return [row for row in sessions if row.get("parent_session_id") == parent_session_id]


def _post_turn(
    http: httpx.Client,
    session_id: str,
    prompt: str,
    *,
    timeout_s: float,
    cancel_after_s: float = 0.0,
    agent_id: str = "",
) -> dict[str, Any]:
    ack = http.post(
        f"/v1/sessions/{session_id}/messages",
        json={
            "parts": [{"type": "text", "text": prompt}],
            **({"agent_id": agent_id} if agent_id else {}),
        },
    )
    ack.raise_for_status()
    user_id = ack.json()["message_id"]
    if cancel_after_s > 0:
        time.sleep(cancel_after_s)
        cancel = http.post(f"/v1/sessions/{session_id}/cancel")
        cancel.raise_for_status()

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


def _turn_agent_id_for_lane(case: DemoCase, lane: str) -> str:
    """Return the per-turn agent override needed for this benchmark lane."""

    if case.turn_agent_id:
        return case.turn_agent_id
    return "main"


def _provider(http: httpx.Client) -> dict[str, Any]:
    try:
        return http.get("/v1/providers/lm").json()
    except Exception as exc:
        return {"error": str(exc)}


def _compact_session(http: httpx.Client, session_id: str, timeout_s: float) -> dict[str, Any]:
    """Run the GACT compaction endpoint and return an audit action row."""
    started = time.monotonic()
    try:
        response = http.post(f"/v1/sessions/{session_id}/compact", json={}, timeout=timeout_s)
        payload = response.json()
        return {
            "type": "compact",
            "ok": response.status_code == 200 and payload.get("compacted") is True,
            "status_code": response.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "result": payload,
        }
    except Exception as exc:
        return {
            "type": "compact",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": repr(exc),
        }


def _swap_provider_for_case(http: httpx.Client, case: DemoCase, timeout_s: float) -> dict[str, Any]:
    """Swap the live LM provider using a preset, then wait for readiness."""
    started = time.monotonic()
    provider_info = _provider(http)
    preset = next(
        (
            row
            for row in provider_info.get("presets", [])
            if row.get("id") == case.provider_swap_preset_id
        ),
        None,
    )
    if not preset:
        return {
            "type": "provider_swap",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": f"provider preset not found: {case.provider_swap_preset_id}",
        }
    model = case.provider_swap_model or preset.get("suggested_model", "")
    payload = {
        "provider": preset.get("provider", ""),
        "api_base": preset.get("api_base", ""),
        "model": model,
        "api_key": "",
        "temperature": 0.0,
        "max_tokens": 4096,
    }
    try:
        response = http.put("/v1/providers/lm", json=payload)
        response.raise_for_status()
        first = response.json()
        deadline = time.monotonic() + timeout_s
        current = first
        while time.monotonic() < deadline:
            current = _provider(http)
            if current.get("state") == "ready":
                return {
                    "type": "provider_swap",
                    "ok": True,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "preset_id": case.provider_swap_preset_id,
                    "model": current.get("model") or model,
                    "result": current,
                }
            if current.get("state") == "error":
                return {
                    "type": "provider_swap",
                    "ok": False,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "preset_id": case.provider_swap_preset_id,
                    "model": model,
                    "result": current,
                }
            time.sleep(1.0)
        return {
            "type": "provider_swap",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "preset_id": case.provider_swap_preset_id,
            "model": model,
            "error": "provider swap did not reach ready before timeout",
            "result": current,
        }
    except Exception as exc:
        return {
            "type": "provider_swap",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "preset_id": case.provider_swap_preset_id,
            "model": model,
            "error": repr(exc),
        }


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
        "outcome": result.outcome,
        "selected_agent": result.selected_agent,
        "routing_decision": _routing_decision(result.message),
        "tools_called": result.tools,
        "tool_names": result.tool_names,
        "data_files": result.data_files,
        "expert_handoffs": result.expert_handoffs,
        "handoff_agent_ids": result.handoff_agent_ids,
        "handoff_event_count": result.handoff_event_count,
        "visible_event_count": result.visible_event_count,
        "child_sessions": result.child_sessions,
        "setup_turn_count": len(result.setup_messages),
        "setup_message_ids": [row.get("id") for row in result.setup_messages],
        "actions": result.actions,
        "artifacts": result.artifacts,
        "artifact_evidence": result.artifact_evidence,
        "route_graph": result.route_graph,
        "route_metrics": result.route_metrics,
        "error_info": result.message.get("error_info"),
        "partial_error": result.partial_error,
        "stop_reason": result.message.get("stop_reason"),
        "provider": result.provider,
        "stream_source": result.stream_source,
        "stream_fallback": result.stream_fallback,
        "routing_mode": result.case.routing_mode,
        "forbidden_route_sources": list(result.case.forbidden_route_sources),
        "benchmark_lane": result.benchmark_lane,
        "complexity_score": result.complexity_score,
        "answer_excerpt": result.text[:1200],
        "complexity_tags": list(result.case.complexity_tags),
    }


def _result_from_case_row(row: dict[str, Any]) -> DemoResult:
    """Rehydrate a recorded JSONL evidence row for markdown report rendering."""
    case = DemoCase(
        case_id=str(row.get("case") or ""),
        title=str(row.get("title") or row.get("case") or ""),
        category=str(row.get("category") or ""),
        session_group=str(row.get("session_group") or row.get("case") or "recorded"),
        prompt=str(row.get("prompt") or ""),
        expected=str(row.get("expected") or ""),
        why=str(row.get("why") or ""),
        routing_mode=str(row.get("routing_mode") or "auto"),
        expects_error=str(row.get("outcome") or "") == "expected_error",
        expects_cancelled=str(row.get("outcome") or "") == "cancelled",
        complexity_tags=tuple(str(tag) for tag in row.get("complexity_tags", []) or []),
        forbidden_route_sources=tuple(
            str(source) for source in row.get("forbidden_route_sources", []) or []
        ),
    )
    routing = dict(row.get("routing_decision") or {})
    if routing and routing.get("type") != "routing_decision":
        routing["type"] = "routing_decision"
    message = {
        "parts": [
            routing,
            {"type": "text", "text": str(row.get("answer_excerpt") or "")},
            {"type": "text", "text": "\n".join(str(path) for path in row.get("artifacts", []) or [])},
        ],
        "metadata": {
            "tools_called": row.get("tools_called") or [],
            "expert_handoffs": row.get("expert_handoffs") or [],
            "stream_source": row.get("stream_source") or "",
            "stream_fallback": row.get("stream_fallback") or {},
        },
        "error_info": row.get("error_info"),
        "stop_reason": row.get("stop_reason"),
    }
    setup_messages = [{"id": message_id} for message_id in row.get("setup_message_ids", []) or []]
    return DemoResult(
        case=case,
        session_id=str(row.get("session_id") or ""),
        elapsed_s=float(row.get("elapsed_s") or 0.0),
        message=message,
        provider=dict(row.get("provider") or {}),
        child_sessions=list(row.get("child_sessions") or []),
        setup_messages=setup_messages,
        actions=list(row.get("actions") or []),
        benchmark_lane=str(row.get("benchmark_lane") or "recorded"),
    )


def render_report_from_jsonl(output_jsonl: Path, report_path: Path) -> None:
    """Render a markdown report from an existing benchmark JSONL evidence file."""
    rows: list[dict[str, Any]] = []
    for line in output_jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    results = [_result_from_case_row(row) for row in rows]
    report_path.write_text(_render_report(results, output_jsonl.resolve()), encoding="utf-8")


def _make_cases(manifest: dict[str, Any]) -> list[DemoCase]:
    h5 = manifest["hdf5"]["path"]
    parquet = manifest["parquet"]["path"]
    dirty = manifest["parquet"]["dirty_path"]
    csv_path = manifest["csv"]["path"]
    adios = manifest["adios"]["path"]
    fasta = manifest["genomics"]["fasta_path"]
    vcf = manifest["genomics"]["vcf_path"]
    cif = manifest["materials"]["cif_path"]
    geojson = manifest["geospatial"]["geojson_path"]
    png = manifest["imaging"]["png_path"]
    mzml = manifest["mass_spec"]["mzml_path"]
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
            turn_agent_id="data",
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
            case_id="context_pressure_compaction_followup",
            title="Context pressure plus explicit compaction",
            category="memory-hardening",
            session_group="context_pressure",
            expected_actions=("compact",),
            expected_terms=("electron_temperature", "anomaly_score"),
            setup_prompts=(
                (
                    f"Build a detailed evidence note from {h5}: include every dataset name, "
                    "shape, units, compression, and at least one risk or follow-up check."
                ),
                (
                    f"Now add a detailed evidence note from {parquet}: include row-group facts, "
                    "schema, and statistics for temperature_k, pressure_pa, humidity_pct, "
                    "vibration_mm_s, and anomaly_score."
                ),
                (
                    f"Now add a detailed evidence note from {csv_path}: include the event columns, "
                    "status semantics, operator notes, and any timestamp caveats."
                ),
                (
                    f'Now add a detailed evidence note from the BP5 run at "{adios}": include '
                    "container/profiling information and dependency caveats."
                ),
            ),
            compact_before_prompt=True,
            timeout_s=900.0,
            complexity_tags=("context-pressure", "compaction", "memory", "multi-turn"),
            prompt=(
                "After the compaction step, use the retained evidence to decide whether the "
                "experiment looks ready for collaborator review. Cite the strongest evidence "
                "from the HDF5, Parquet, CSV, and BP5 stages, and name what still needs checking."
            ),
            expected=(
                "A long multi-turn session is compacted, then CLIO answers from retained evidence "
                "instead of losing prior HDF5/Parquet/CSV/BP5 conclusions."
            ),
            why=(
                "This stresses context retention and makes compaction a first-class benchmark "
                "event rather than an untested UI command."
            ),
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
            case_id="csv_status_visual_summary",
            title="CSV status distribution chart",
            category="visualization",
            session_group="csv_visual",
            expected_agent="visualization",
            expected_tools=("plot_bar_chart",),
            expected_terms=(".png", "status"),
            setup_prompts=(
                (
                    f"Inspect the CSV event stream at {csv_path}. Record the columns and "
                    "which field represents event status."
                ),
            ),
            timeout_s=620.0,
            complexity_tags=(
                "csv",
                "visualization",
                "artifact",
                "multi-turn",
                "analysis-to-visualization",
            ),
            prompt=(
                "Create a PNG bar chart of the event status distribution from the CSV stream "
                "we just inspected. Tell me where it was saved and what field was plotted."
            ),
            expected="Visualization resolves the prior CSV context and plots the status field.",
            why=(
                "Exercises a CSV analysis-to-visualization handoff and verifies that charting "
                "is not limited to Parquet dashboards."
            ),
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
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
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
            case_id="cross_file_dirty_quality_gate_nanoagents",
            title="Dirty cross-file quality gate",
            category="multi-agent",
            session_group="cross_file_dirty",
            expected_agent="analysis",
            expected_terms=("data_validator", "analysis_validator", "csv_validator"),
            min_children=3,
            min_tool_calls=6,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=(
                "nanoagents",
                "tier-3",
                "dirty-data",
                "quality-gate",
                "multi-file",
            ),
            prompt=(
                f"Before I share this run, build a quality gate across {h5}, {dirty}, "
                f'{csv_path}, and "{adios}". I need to know what each file proves, where '
                "the dirty tabular export is risky, and which checks block collaborator handoff."
            ),
            expected=(
                "Analysis coordinates tool-backed child workers over HDF5, dirty Parquet, "
                "CSV, and BP5 evidence."
            ),
            why=(
                "Adds a harder cross-file case where one source is intentionally dirty and "
                "the user asks for a review gate rather than a generic summary."
            ),
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
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
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
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
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
            case_id="dirty_quality_dashboard_multi_turn",
            title="Dirty data dashboard after quality review",
            category="visualization",
            session_group="dirty_visual",
            expected_agent="visualization",
            expected_tools=("plot_summary",),
            expected_terms=(".png", "dashboard"),
            setup_prompts=(
                (
                    f"Review the suspicious Parquet export at {dirty}. Record the schema, "
                    "quality_flag, temperature_k, pressure_pa, and any quality concerns."
                ),
            ),
            timeout_s=620.0,
            complexity_tags=(
                "dirty-data",
                "visualization",
                "artifact",
                "multi-turn",
                "quality-review",
            ),
            prompt=(
                "Create a compact dashboard PNG for the dirty Parquet export we just reviewed. "
                "Use it to support the quality review, and tell me where the artifact was saved."
            ),
            expected=(
                "Visualization resolves the reviewed dirty Parquet file from memory and creates "
                "a real dashboard artifact."
            ),
            why=(
                "Stresses multi-turn analysis-to-visualization over intentionally dirty data, "
                "not only clean demo fixtures."
            ),
        ),
        DemoCase(
            case_id="ndp_catalog_discovery",
            title="NDP catalog discovery",
            category="external-catalog",
            session_group="ndp",
            expected_agent=("data", "ndp_catalog"),
            expected_tool_prefixes=("ndp_",),
            expected_handoff_agents=("ndp_catalog",),
            expected_terms=("dataset",),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("ndp", "clio-kit", "external-mcp"),
            prompt=(
                "Find a few NOAA or climate-related datasets in the National Data Platform "
                "catalog that might complement this facility data. Summarize what you found "
                "and what I should verify before download."
            ),
            expected="Data expert delegates discovery to NDP tools through the CLIO gateway.",
            why=(
                "Exercises external catalog discovery as a data-stage capability, before "
                "analysis consumes staged data."
            ),
        ),
        DemoCase(
            case_id="ndp_seismic_waveform_to_plot",
            title="NDP seismic waveform discovery to plot",
            category="hierarchical-science",
            session_group="ndp_seismic",
            expected_agent=("visualization", "analysis", "data", "ndp_catalog"),
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_tool_prefix_groups=(("ndp_", "sac_"), ("ndp_",)),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_handoff_agent_groups=(),
            expected_terms=("SAC", ".png"),
            expected_term_groups=(),
            min_artifacts=1,
            timeout_s=900.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=(
                "ndp",
                "earthscience",
                "tier-3",
                "sac",
                "data-analysis-visualization",
                "artifact",
            ),
            prompt=(
                "Find a bounded seismic waveform dataset from a seismological or "
                "Earth-science organization in the National Data Platform. Choose a usable "
                "resource, stage it if it is small enough, inspect the waveform content, "
                "compute representative trace statistics, and produce a plot artifact. If a "
                "candidate is too large or unavailable, surface that as the result instead "
                "of inventing a plot."
            ),
            expected=(
                "CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform "
                "resource, analyzes SAC traces through sac_format, and creates a PNG plot."
            ),
            why=(
                "This is the core hierarchical science demo: provider discovery, data "
                "access, format-specific analysis, and visualization without the user naming "
                "internal agents."
            ),
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
            case_id="genomics_reference_variant_review",
            title="Genomics reference and variant review",
            category="genomics",
            session_group="genomics",
            expected_agent="genomics",
            expected_tools=("genomics_inspect_fasta", "genomics_summarize_vcf"),
            expected_terms=("chrA", "plasmidB", "missense", "frameshift"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("genomics", "fasta", "vcf", "new-domain", "multi-file"),
            prompt=(
                f"Review this synthetic pathogen reference FASTA and variant call file: "
                f"{fasta} and {vcf}. Summarize the reference composition, the variant "
                "types and effects, and what a collaborator should verify before treating "
                "the sample as analysis-ready."
            ),
            expected=(
                "CLIO uses FASTA and VCF genomics tools, then grounds a review in sequence "
                "composition and variant effect evidence."
            ),
            why=(
                "Adds a non-NDP, non-HDF5/Parquet domain that requires new domain tools and "
                "a new expert boundary."
            ),
        ),
        DemoCase(
            case_id="materials_cif_structure_review",
            title="Materials CIF structure review",
            category="materials",
            session_group="materials",
            expected_agent="materials",
            expected_tools=("materials_inspect_cif",),
            expected_terms=("SrTiO3", "P m -3 m", "Sr", "Ti", "O"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("materials", "crystallography", "cif", "new-domain"),
            prompt=(
                f"Review this crystal structure file for collaborator handoff: {cif}. "
                "Summarize the unit cell, symmetry, atom species, and any density or "
                "occupancy checks that should be verified before simulation setup."
            ),
            expected=(
                "CLIO uses CIF materials tools and grounds the review in unit-cell, "
                "space-group, species, and atom-site evidence."
            ),
            why=(
                "Adds a non-NDP materials science domain that requires a new file parser, "
                "tool, and expert route instead of generic text inspection."
            ),
        ),
        DemoCase(
            case_id="geospatial_field_site_review",
            title="Geospatial field-site review",
            category="geospatial",
            session_group="geospatial",
            expected_agent="geospatial",
            expected_tools=("geospatial_inspect_geojson",),
            expected_terms=("north_ridge", "south_valley", "study_boundary", "Polygon"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("geospatial", "geojson", "new-domain"),
            prompt=(
                f"Review this field-site GeoJSON for spatial analysis readiness: {geojson}. "
                "Summarize the feature types, coordinate bounds, key properties, and what "
                "a collaborator should verify before using it in a map overlay."
            ),
            expected=(
                "CLIO uses GeoJSON geospatial tools and grounds the review in feature, "
                "geometry, bounds, and property evidence."
            ),
            why=(
                "Adds a non-NDP geospatial domain with coordinate and geometry semantics, "
                "not just generic JSON text inspection."
            ),
        ),
        DemoCase(
            case_id="microscopy_png_readiness_review",
            title="Microscopy PNG readiness review",
            category="imaging",
            session_group="imaging",
            expected_agent="imaging",
            expected_tools=("imaging_inspect_png",),
            expected_terms=("PNG", "foreground", "intensity", "connected"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("imaging", "png", "microscopy", "new-domain"),
            prompt=(
                f"Review this microscopy-style PNG for collaborator handoff: {png}. "
                "Summarize the image dimensions, intensity range, foreground estimate, "
                "region evidence, and what acquisition metadata should be verified before "
                "quantitative analysis."
            ),
            expected=(
                "CLIO uses PNG imaging tools and grounds the review in dimensions, "
                "intensity, foreground, and region evidence."
            ),
            why=(
                "Adds a binary scientific image domain with pixel and region semantics, "
                "not generic file text or chart-generation behavior."
            ),
        ),
        DemoCase(
            case_id="mass_spec_mzml_qc_review",
            title="Mass spectrometry mzML QC review",
            category="mass_spec",
            session_group="mass_spec",
            expected_agent="mass_spec",
            expected_tools=("mass_spec_inspect_mzml",),
            expected_terms=("mzML", "ms level", "scan=1", "total ion current"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("mass-spec", "mzml", "proteomics", "new-domain"),
            prompt=(
                f"Review this proteomics mzML run for collaborator handoff: {mzml}. "
                "Summarize the spectra, MS-level balance, m/z coverage, intensity/TIC "
                "evidence, and what acquisition metadata should be verified before "
                "peptide-search analysis."
            ),
            expected=(
                "CLIO uses mzML mass spectrometry tools and grounds the review in spectra, "
                "MS levels, m/z range, peak counts, and TIC evidence."
            ),
            why=(
                "Adds a structured XML scientific instrument domain with spectra and "
                "ion-current semantics, not generic XML text inspection."
            ),
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
            turn_agent_id="data",
            prompt=(
                f"Inspect this HDF5 file and tell me what datasets are inside: {missing}. "
                "If the file is unavailable, surface the real error."
            ),
            expected="CLIO returns structured error_info and no normal fake assistant answer.",
            why="Verifies errors are surfaced, not hidden behind canned or repeated text.",
        ),
        DemoCase(
            case_id="missing_csv_error",
            title="Missing CSV error surfacing",
            category="hardening",
            session_group="errors",
            expects_error=True,
            complexity_tags=("error-surfacing", "csv", "no-fake-answer"),
            prompt=(
                f"Read this collaborator CSV and summarize the columns: "
                f"{Path(csv_path).with_name('missing_sensor_events.csv')}. "
                "If it is unavailable, surface the real error rather than guessing."
            ),
            expected="CLIO returns structured error_info for a missing CSV path with no fake answer.",
            why="Adds a second deliberate surfaced-error benchmark outside the HDF5 path.",
        ),
        DemoCase(
            case_id="claude_cancellation_surface",
            title="Claude Code cancellation surface",
            category="provider-hardening",
            session_group="claude_cancel",
            expects_cancelled=True,
            cancel_after_s=0.001,
            timeout_s=240.0,
            complexity_tags=("claude-code", "cancellation", "provider-boundary"),
            prompt=(
                "Cancellation benchmark: prepare a very detailed scientific review plan for "
                f"{h5}, {parquet}, and {csv_path}. Include staged reasoning, validation "
                "checks, schema comparisons, and collaborator handoff notes so this turn should "
                "remain active long enough for the benchmark runner to cancel it."
            ),
            expected="CLIO settles the GACT envelope as a structured cancelled turn.",
            why=(
                "The Claude lane must prove cancellation surfacing separately from successful "
                "tool and planner cases, without claiming hard upstream abort."
            ),
        ),
        DemoCase(
            case_id="provider_swap_memory_followup",
            title="Provider swap preserves session context",
            category="provider-hardening",
            session_group="provider_swap",
            expected_actions=("provider_swap",),
            expected_tool_prefixes=("parquet_",),
            expected_terms=("temperature", "pressure"),
            provider_swap_preset_id="argonne_sophia",
            provider_swap_model="meta-llama/Meta-Llama-3.1-8B-Instruct",
            setup_prompts=(
                (
                    f"Profile {parquet} for a provider-swap test. Record the path, schema, "
                    "and basic temperature_k and pressure_pa facts so a later model can continue."
                ),
            ),
            timeout_s=900.0,
            complexity_tags=("provider-swap", "memory", "alcf", "session-context"),
            prompt=(
                "The provider/model has changed. Continue from the facility Parquet table we just "
                "profiled, compute any statistics you need for temperature and pressure, and tell "
                "me whether the session context survived the swap."
            ),
            expected=(
                "After a live ALCF provider/model swap, CLIO keeps the session coherent and uses "
                "the remembered Parquet context with visible tool evidence."
            ),
            why=(
                "Provider/model swaps have historically destabilized active sessions, so this "
                "turn should catch stale model refs, lost context, and hidden `(no parts)` errors."
            ),
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


_BENCHMARK_LANES: dict[str, tuple[str, ...]] = {
    "all": (),
    "real_orchestrator": (
        "reasoning_cross_file_triage_nanoagents",
        "cross_file_dirty_quality_gate_nanoagents",
        "csv_status_visual_summary",
        "dirty_quality_dashboard_multi_turn",
        "genomics_reference_variant_review",
        "materials_cif_structure_review",
        "geospatial_field_site_review",
        "microscopy_png_readiness_review",
        "mass_spec_mzml_qc_review",
        "ndp_catalog_discovery",
        "ndp_seismic_waveform_to_plot",
        "reasoning_adios_bp5_container",
    ),
    "claude_code": (
        "workflow_hdf5_overview",
        "workflow_parquet_profile",
        "reasoning_cross_file_triage_nanoagents",
        "missing_hdf5_error",
        "claude_cancellation_surface",
    ),
}


def _lane_title(lane: str) -> str:
    if lane == "claude_code":
        return "CLIO Claude Code Real-Provider Benchmark Report"
    if lane == "real_orchestrator":
        return "CLIO Real-Orchestrator Benchmark Report"
    if lane == "all":
        return "CLIO Full Real-Provider Benchmark Report"
    return "CLIO ALCF Demo Benchmark Report"


def _select_cases(
    cases: list[DemoCase],
    *,
    lane: str,
    case_ids: tuple[str, ...],
) -> tuple[list[DemoCase], list[str]]:
    """Apply provider-lane and explicit case filters."""

    if lane not in _BENCHMARK_LANES:
        return [], [f"unknown benchmark lane: {lane}"]
    if lane != "all":
        wanted = set(_BENCHMARK_LANES[lane])
        cases = [case for case in cases if case.case_id in wanted]
    if case_ids:
        requested = set(case_ids)
        missing = sorted(requested - {case.case_id for case in cases})
        cases = [case for case in cases if case.case_id in requested]
        return cases, missing
    return cases, []


def run_benchmark(
    base_url: str,
    data_dir: Path,
    output_jsonl: Path,
    report_path: Path,
    *,
    case_delay_s: float = 0.0,
    require_stress_criteria: bool = False,
    require_lane_criteria: bool = False,
    lane: str = "real_orchestrator",
    case_ids: tuple[str, ...] = (),
) -> int:
    """Run demo cases and write JSONL plus a markdown report."""
    manifest = create_benchmark_data(data_dir)
    cases = _make_cases(manifest)
    cases, missing = _select_cases(cases, lane=lane, case_ids=case_ids)
    if missing:
        print(f"Unknown case id(s): {', '.join(missing)}", file=sys.stderr)
        return 2
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2
    results: list[DemoResult] = []
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=base_url, timeout=90.0) as http:
        health = http.get("/v1/health")
        health.raise_for_status()
        session_ids = _create_sessions(http, cases)
        with output_jsonl.open("w", encoding="utf-8") as log:
            for index, case in enumerate(cases, start=1):
                session_id = session_ids[_session_key(case)]
                before_children = {child["id"] for child in _children(http, session_id)}
                print(f"[{index}/{len(cases)}] {case.case_id}: {case.title}", flush=True)
                setup_messages: list[dict[str, Any]] = []
                actions: list[dict[str, Any]] = []
                for setup_index, setup_prompt in enumerate(case.setup_prompts, start=1):
                    print(f"  setup turn {setup_index}/{len(case.setup_prompts)}", flush=True)
                    setup_messages.append(
                        _post_turn(
                            http,
                            session_id,
                            setup_prompt,
                            timeout_s=case.timeout_s,
                            agent_id=_turn_agent_id_for_lane(case, lane),
                        )
                    )
                    if case_delay_s > 0:
                        time.sleep(case_delay_s)
                if case.compact_before_prompt:
                    action = _compact_session(http, session_id, timeout_s=case.timeout_s)
                    actions.append(action)
                    print(
                        f"  compact {'ok' if action.get('ok') else 'failed'} "
                        f"elapsed={action.get('elapsed_s')}s",
                        flush=True,
                    )
                if case.provider_swap_preset_id:
                    action = _swap_provider_for_case(http, case, timeout_s=case.timeout_s)
                    actions.append(action)
                    print(
                        f"  provider swap {'ok' if action.get('ok') else 'failed'} "
                        f"model={action.get('model') or '-'} elapsed={action.get('elapsed_s')}s",
                        flush=True,
                    )
                provider = _provider(http)
                started = time.monotonic()
                message = _post_turn(
                    http,
                    session_id,
                    case.prompt,
                    timeout_s=case.timeout_s,
                    cancel_after_s=case.cancel_after_s,
                    agent_id=_turn_agent_id_for_lane(case, lane),
                )
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
                    setup_messages=setup_messages,
                    actions=actions,
                    benchmark_lane=lane,
                )
                results.append(result)
                log.write(json.dumps(_case_row(result), ensure_ascii=False, default=str) + "\n")
                log.flush()
                status = result.outcome.upper()
                print(
                    f"  {status} agent={result.selected_agent or '-'} "
                    f"source={result.route_source or '-'} "
                    f"tools={','.join(result.tool_names) or '-'} "
                    f"children={len(result.child_sessions)} elapsed={elapsed_s:.1f}s",
                    flush=True,
                )
                if case_delay_s > 0 and index < len(cases):
                    time.sleep(case_delay_s)

    report_path.write_text(_render_report(results, output_jsonl), encoding="utf-8")
    cases_passed = all(result.passed for result in results)
    audit_passed = all(item["passed"] for item in _stress_audit(results))
    lane_passed = all(item["passed"] for item in _provider_lane_audit(results, lane))
    if require_lane_criteria:
        return 0 if cases_passed and lane_passed else 1
    if require_stress_criteria:
        return 0 if cases_passed and audit_passed else 1
    return 0 if cases_passed else 1


def _stress_audit(results: list[DemoResult]) -> list[dict[str, Any]]:
    """Evaluate the campaign against the documented stress benchmark standard."""
    complex_demos = [result for result in results if result.complexity_score >= 25]
    long_or_many = [
        result
        for result in results
        if result.elapsed_s >= 120.0 or result.visible_event_count >= 10
    ]
    tier3_or_nano = [
        result
        for result in results
        if result.child_sessions
        or {"ndp_catalog", "sac_format"}.intersection(result.handoff_agent_ids)
    ]
    artifact_runs = [result for result in results if result.artifacts]
    expected_errors = [result for result in results if result.outcome == "expected_error"]
    compaction_runs = [
        result
        for result in results
        if any(action.get("type") == "compact" and action.get("ok") for action in result.actions)
    ]
    provider_swaps = [
        result
        for result in results
        if any(
            action.get("type") == "provider_swap" and action.get("ok") for action in result.actions
        )
    ]
    return [
        {
            "criterion": "at least ten complex collaborator-grade demos",
            "observed": len(complex_demos),
            "required": 10,
            "passed": len(complex_demos) >= 10,
        },
        {
            "criterion": "at least five long or high-event stress cases",
            "observed": len(long_or_many),
            "required": 5,
            "passed": len(long_or_many) >= 5,
            "details": [
                f"{result.case.case_id} ({result.elapsed_s:.1f}s, {result.visible_event_count} events)"
                for result in long_or_many
            ],
        },
        {
            "criterion": "at least three cases with tier-3 agents or nanoagents",
            "observed": len(tier3_or_nano),
            "required": 3,
            "passed": len(tier3_or_nano) >= 3,
        },
        {
            "criterion": "at least three visualization artifacts from analyzed data",
            "observed": len(artifact_runs),
            "required": 3,
            "passed": len(artifact_runs) >= 3,
        },
        {
            "criterion": "at least two deliberate surfaced-error cases",
            "observed": len(expected_errors),
            "required": 2,
            "passed": len(expected_errors) >= 2,
        },
        {
            "criterion": "at least one context-pressure or compaction case",
            "observed": len(compaction_runs),
            "required": 1,
            "passed": len(compaction_runs) >= 1,
        },
        {
            "criterion": "at least one provider/model-swap stress case",
            "observed": len(provider_swaps),
            "required": 1,
            "passed": len(provider_swaps) >= 1,
        },
    ]


def _provider_lane_audit(results: list[DemoResult], lane: str) -> list[dict[str, Any]]:
    """Evaluate provider-specific evidence requirements."""

    by_case = {result.case.case_id: result for result in results}

    def passed(case_id: str) -> bool:
        result = by_case.get(case_id)
        return bool(result and result.passed)

    def case_result(case_id: str) -> DemoResult | None:
        return by_case.get(case_id)

    if lane == "real_orchestrator":
        forbidden = [
            result
            for result in results
            if result.route_source in result.case.forbidden_route_sources
        ]
        passing_results = [result for result in results if result.passed]
        missing_route_evidence = [
            result
            for result in passing_results
            if result.passed
            and (
                not result.route_graph["nodes"]
                or result.route_metrics["expert_depth"] <= 0
                or (result.case.expected_tools and result.route_metrics["tool_call_count"] <= 0)
            )
        ]
        artifact_expected = [
            result
            for result in results
            if (
                result.case.min_artifacts > 0
                or result.passed
                and (
                ".png" in result.text.lower()
                or any(path.lower().endswith(".png") for path in result.artifacts)
                or result.route_metrics["artifact_count"] > 0
                )
            )
        ]
        missing_artifact_evidence = [
            result
            for result in artifact_expected
            if not result.artifact_evidence
            or any(
                not row.get("exists") or int(row.get("size_bytes") or 0) <= 0
                for row in result.artifact_evidence
            )
        ]
        ndp_waveform = case_result("ndp_seismic_waveform_to_plot")
        ndp_full_chain = bool(
            ndp_waveform
            and ndp_waveform.passed
            and any(name.startswith("sac_") for name in ndp_waveform.tool_names)
            and ndp_waveform.artifact_evidence
            and all(
                row.get("exists") and int(row.get("size_bytes") or 0) > 0
                for row in ndp_waveform.artifact_evidence
            )
        )
        ndp_details = []
        if ndp_waveform and ndp_waveform.passed and not ndp_full_chain:
            ndp_details.append("NDP case passed without verified SAC/PNG artifact evidence")
        return [
            {
                "criterion": "all selected cases avoid shortcut route sources",
                "observed": len(results) - len(forbidden),
                "required": len(results),
                "passed": not forbidden,
                "details": [
                    f"{result.case.case_id}: route_source={result.route_source}"
                    for result in forbidden
                ],
            },
            {
                "criterion": "passing cases include structured route/tool evidence",
                "observed": len(passing_results) - len(missing_route_evidence),
                "required": len(passing_results),
                "passed": not missing_route_evidence,
                "details": [
                    f"{result.case.case_id}: route_metrics={result.route_metrics}"
                    for result in missing_route_evidence
                ],
            },
            {
                "criterion": "artifact-producing cases verify artifacts on disk",
                "observed": len(artifact_expected) - len(missing_artifact_evidence),
                "required": len(artifact_expected),
                "passed": not missing_artifact_evidence,
                "details": [
                    f"{result.case.case_id}: artifact_evidence={result.artifact_evidence}"
                    for result in missing_artifact_evidence
                ],
            },
            {
                "criterion": "planner multi-file hierarchy case passes",
                "observed": int(passed("reasoning_cross_file_triage_nanoagents")),
                "required": 1,
                "passed": passed("reasoning_cross_file_triage_nanoagents"),
            },
            {
                "criterion": "dirty cross-file quality gate passes",
                "observed": int(passed("cross_file_dirty_quality_gate_nanoagents")),
                "required": 1,
                "passed": passed("cross_file_dirty_quality_gate_nanoagents"),
            },
            {
                "criterion": "NDP waveform benchmark reaches verified SAC/PNG artifact",
                "observed": int(passed("ndp_seismic_waveform_to_plot")),
                "required": 1,
                "passed": bool(ndp_full_chain),
                "details": ndp_details,
            },
            {
                "criterion": "NDP full SAC/PNG chain verified",
                "observed": int(ndp_full_chain),
                "required": 1,
                "passed": bool(ndp_full_chain),
                "details": [] if ndp_full_chain else ["full SAC/PNG path not reached in this run"],
            },
        ]

    if lane != "claude_code":
        return []

    provider_rows = [
        result
        for result in results
        if str(result.provider.get("provider") or "").strip()
        and str(result.provider.get("model") or "").strip()
    ]
    stream_rows = [result for result in results if result.stream_source]
    return [
        {
            "criterion": "provider/model recorded for every case",
            "observed": len(provider_rows),
            "required": len(results),
            "passed": len(provider_rows) == len(results),
        },
        {
            "criterion": "planner JSON/routing reliability case passes",
            "observed": int(passed("reasoning_cross_file_triage_nanoagents")),
            "required": 1,
            "passed": passed("reasoning_cross_file_triage_nanoagents"),
        },
        {
            "criterion": "tool-call argument generation cases pass",
            "observed": sum(
                int(passed(case_id))
                for case_id in ("workflow_hdf5_overview", "workflow_parquet_profile")
            ),
            "required": 2,
            "passed": passed("workflow_hdf5_overview") and passed("workflow_parquet_profile"),
        },
        {
            "criterion": "stream provenance captured",
            "observed": len(stream_rows),
            "required": len(results),
            "passed": len(stream_rows) == len(results),
            "details": [
                f"{result.case.case_id}: stream_source={result.stream_source or '-'}"
                + (
                    f", fallback={result.stream_fallback.get('reason')}"
                    if result.stream_fallback
                    else ""
                )
                for result in results
            ],
        },
        {
            "criterion": "cancellation surfaces as structured cancelled turn",
            "observed": int(passed("claude_cancellation_surface")),
            "required": 1,
            "passed": passed("claude_cancellation_surface"),
        },
        {
            "criterion": "provider-specific failures stay visible",
            "observed": int(passed("missing_hdf5_error")),
            "required": 1,
            "passed": passed("missing_hdf5_error"),
        },
    ]


def _render_report(results: list[DemoResult], output_jsonl: Path) -> str:
    clean_passes = sum(1 for result in results if result.outcome == "pass")
    expected_errors = sum(1 for result in results if result.outcome == "expected_error")
    expected_cancelled = sum(1 for result in results if result.outcome == "cancelled")
    partials = sum(1 for result in results if result.outcome == "partial")
    failures = sum(1 for result in results if result.outcome == "fail")
    lane = results[0].benchmark_lane if results else "default"
    audit = _stress_audit(results)
    provider_audit = _provider_lane_audit(results, lane)
    best = sorted(results, key=lambda result: result.complexity_score, reverse=True)[:10]
    max_elapsed = max(results, key=lambda result: result.elapsed_s, default=None)
    max_depth = max(results, key=lambda result: result.route_metrics["expert_depth"], default=None)
    max_branch = max(results, key=lambda result: result.route_metrics["branch_count"], default=None)
    all_tools = sorted({tool for result in results for tool in result.tool_names if tool})
    all_files = sorted({path for result in results for path in result.data_files})
    artifact_rows = [
        row for result in results for row in result.artifact_evidence if row.get("path")
    ]
    verified_artifacts = [row for row in artifact_rows if row.get("exists") and row.get("size_bytes", 0) > 0]
    lines = [
        f"# {_lane_title(lane)}",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Evidence JSONL: `{output_jsonl}`",
        f"Benchmark lane: `{lane}`",
        "",
        (
            "This is a CLIO session-evidence audit. It is produced from real "
            "session JSONL rows and should be reviewed as prompt, route, tool, "
            "artifact, error, and final-answer evidence. Pytest coverage only "
            "guards the harness and tools; it is not the benchmark result."
        ),
        "",
        (
            f"Result: {clean_passes}/{len(results)} clean passes, "
            f"{expected_errors} expected surfaced errors, "
            f"{expected_cancelled} expected cancellations, {partials} partial recoveries, "
            f"{failures} failures."
        ),
        "",
        "Extended stress coverage: "
        + (
            "meets the optional extended stress targets."
            if all(item["passed"] for item in audit)
            else "has optional gaps outside the per-lane pass/fail gate."
        ),
        "",
        "## Extended Stress Coverage Audit",
        "",
        "| Criterion | Observed | Required | Status |",
        "| --- | ---: | ---: | --- |",
    ]
    for item in audit:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["criterion"],
                    str(item["observed"]),
                    str(item["required"]),
                    "pass" if item["passed"] else "gap",
                ]
            )
            + " |"
        )
    audit_details = [detail for item in audit for detail in item.get("details", [])]
    if audit_details:
        lines.extend(["", "High-event or long-running cases:", ""])
        lines.extend(f"- {detail}" for detail in audit_details)
    lines.extend(
        [
            "",
            "## Evidence Summary",
            "",
            f"- Max elapsed case: {_result_metric_label(max_elapsed, 'elapsed')}",
            f"- Max expert depth: {_result_metric_label(max_depth, 'expert_depth')}",
            f"- Max branch fanout: {_result_metric_label(max_branch, 'branch_count')}",
            f"- Unique tools used: {', '.join(all_tools) if all_tools else 'none'}",
            f"- Data/input files referenced: {len(all_files)}",
            f"- Artifacts verified on disk: {len(verified_artifacts)}/{len(artifact_rows)}",
        ]
    )
    if provider_audit:
        lines.extend(
            [
                "",
                "## Provider Lane Audit",
                "",
                "| Criterion | Observed | Required | Status |",
                "| --- | ---: | ---: | --- |",
            ]
        )
        for item in provider_audit:
            lines.append(
                "| "
                + " | ".join(
                    [
                        item["criterion"],
                        str(item["observed"]),
                        str(item["required"]),
                        "pass" if item["passed"] else "gap",
                    ]
                )
                + " |"
            )
        provider_details = [
            detail for item in provider_audit for detail in item.get("details", [])
        ]
        if provider_details:
            lines.extend(["", "Provider evidence details:", ""])
            lines.extend(f"- {detail}" for detail in provider_details)
    lines.extend(
        [
            "",
            "## All Cases",
            "",
            "| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
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
                    result.outcome,
                    result.selected_agent or "-",
                    _handoff_summary(result) or "-",
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
        handoff_text = _handoff_summary(result) or "none"
        artifact_text = ", ".join(result.artifacts) or "none"
        artifact_evidence_text = ", ".join(
            f"{row['path']} ({'ok' if row.get('exists') and row.get('size_bytes', 0) > 0 else 'missing'}, {row.get('size_bytes', 0)} B)"
            for row in result.artifact_evidence
        ) or "none"
        action_text = ", ".join(
            f"{action.get('type')}={'ok' if action.get('ok') else 'failed'}"
            for action in result.actions
        )
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
                f"Status: {result.outcome}",
                f"Selected agent: `{result.selected_agent or '-'}`",
                f"Provider/model: {_provider_model_summary(result.provider)}",
                f"Provider settings: {_provider_settings_summary(result.provider)}",
                f"Route graph: {_route_graph_summary(result)}",
                f"Route metrics: depth={result.route_metrics['expert_depth']}, branches={result.route_metrics['branch_count']}, tools={result.route_metrics['tool_call_count']}",
                f"Expert handoffs: {handoff_text}",
                f"Tools: {tool_text}",
                f"Data/input files: {', '.join(result.data_files) or 'none'}",
                f"Setup turns: {len(result.setup_messages)}",
                f"Actions: {action_text or 'none'}",
                f"Child sessions: {child_text or 'none'}",
                f"Artifacts: {artifact_text}",
                f"Artifact evidence: {artifact_evidence_text}",
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

    lines.extend(
        [
            "## Failures Fixed During This Campaign",
            "",
            "- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.",
            "- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.",
            "- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.",
            "- Visualization-intent follow-ups could route to analysis or a data tool even when the user asked for a chart/dashboard; file-grounded visual artifact requests are promoted to the visualization expert.",
            "- Direct planner-selected NDP and Parquet/statistical tool actions could flatten expert ownership; NDP catalog work is promoted to the nested `ndp_catalog` expert, and statistical Parquet triage is promoted to `analysis`.",
            "- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.",
            "",
        ]
    )

    lines.extend(
        [
            "## Remaining Caveats",
            "",
            "- This report is evidence for the recorded provider/session run, not a guarantee that provider availability, model latency, token freshness, or external data services will be identical later.",
            "- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.",
            "- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.",
            "",
        ]
    )
    if expected_errors or expected_cancelled:
        lines.extend(
            [
                "Deliberate failure/cancellation cases are counted as successful hardening cases only when they return structured errors without normal-looking fake assistant text.",
                "",
            ]
        )

    partial_results = [result for result in results if result.outcome == "partial"]
    if partial_results:
        lines.extend(["## Partial Recovery Caveats", ""])
        for result in partial_results:
            details = (result.partial_error or {}).get("details") or {}
            lines.extend(
                [
                    f"- `{result.case.case_id}`: {result.partial_error.get('message') if result.partial_error else 'partial recovery'}",
                    f"  stage={details.get('stage')}, tools={', '.join(result.tool_names) or '-'}",
                ]
            )

    failed_results = [result for result in results if result.outcome == "fail"]
    if failed_results:
        lines.extend(["## Failures To Investigate", ""])
        for result in failed_results:
            lines.extend(
                [
                    f"- `{result.case.case_id}`: expected {result.case.expected}",
                    f"  observed agent={result.selected_agent or '-'}, "
                    f"tools={', '.join(result.tool_names) or '-'}, "
                    f"error={result.message.get('error_info')}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def _result_metric_label(result: DemoResult | None, metric: str) -> str:
    """Return a compact campaign metric label."""
    if result is None:
        return "none"
    if metric == "elapsed":
        return f"`{result.case.case_id}` ({result.elapsed_s:.1f}s)"
    value = result.route_metrics.get(metric, 0)
    return f"`{result.case.case_id}` ({value})"


def _handoff_summary(result: DemoResult) -> str:
    """Return compact handoff display text with repeated event counts."""
    counts: dict[str, int] = {}
    for row in result.expert_handoffs:
        agent_id = str(row.get("agent_id") or "")
        if not agent_id:
            continue
        counts[agent_id] = counts.get(agent_id, 0) + 1
    parts = [
        agent_id if count == 1 else f"{agent_id} x{count}" for agent_id, count in counts.items()
    ]
    return ", ".join(parts)


def _provider_model_summary(provider: dict[str, Any]) -> str:
    """Return compact provider/model evidence for the report."""
    provider_id = str(provider.get("provider") or "-")
    model = str(provider.get("model") or "-")
    api_base = str(provider.get("api_base") or "-")
    return f"`{provider_id}` / `{model}` via `{api_base}`"


def _provider_settings_summary(provider: dict[str, Any]) -> str:
    """Return compact generation settings evidence for the report."""
    return (
        f"temperature={provider.get('temperature', '-')}, "
        f"max_tokens={provider.get('max_tokens', '-')}, "
        f"context_length={provider.get('context_length', '-')}, "
        f"thinking_budget={provider.get('thinking_budget', '-')}"
    )


def _route_graph_summary(result: DemoResult) -> str:
    """Return a compact route graph from selected agent, handoffs, and children."""
    nodes: list[str] = []
    if result.selected_agent:
        nodes.append(result.selected_agent)
    for agent_id in result.handoff_agent_ids:
        if not nodes or nodes[-1] != agent_id:
            nodes.append(agent_id)
    child_names = [
        str(child.get("title") or child.get("name") or child.get("id") or "")
        for child in result.child_sessions
    ]
    child_names = [name for name in child_names if name]
    graph = " -> ".join(nodes) if nodes else "-"
    if child_names:
        graph += " -> [" + ", ".join(child_names) + "]"
    return graph


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
        default=Path("tmp/clio-real-orchestrator-benchmark.jsonl"),
        help="Output evidence JSONL path.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("benchmark/REAL_ORCHESTRATOR_REPORT.md"),
        help="Output markdown report path.",
    )
    parser.add_argument(
        "--render-existing-jsonl",
        type=Path,
        default=None,
        help="Render a report from an existing benchmark JSONL file without running cases.",
    )
    parser.add_argument(
        "--case-delay-s",
        type=float,
        default=0.0,
        help="Optional cooldown between cases for rate-limited real providers.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Run only the named case id. May be supplied multiple times.",
    )
    parser.add_argument(
        "--require-stress-criteria",
        action="store_true",
        help="Exit non-zero unless the documented stress coverage audit passes.",
    )
    parser.add_argument(
        "--lane",
        choices=sorted(_BENCHMARK_LANES),
        default="real_orchestrator",
        help="Benchmark lane to run. Defaults to the strict real-orchestrator lane.",
    )
    parser.add_argument(
        "--require-lane-criteria",
        action="store_true",
        help="Exit non-zero unless the selected provider lane audit passes.",
    )
    args = parser.parse_args()
    if args.render_existing_jsonl is not None:
        render_report_from_jsonl(args.render_existing_jsonl.resolve(), args.report.resolve())
        raise SystemExit(0)
    raise SystemExit(
        run_benchmark(
            args.base_url.rstrip("/"),
            args.data_dir.resolve(),
            args.output_jsonl.resolve(),
            args.report.resolve(),
            case_delay_s=max(0.0, args.case_delay_s),
            require_stress_criteria=args.require_stress_criteria,
            require_lane_criteria=args.require_lane_criteria,
            lane=args.lane,
            case_ids=tuple(args.case),
        )
    )


if __name__ == "__main__":
    main()
