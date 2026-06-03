#!/usr/bin/env python3
"""Run CLIO/GACT demo benchmarks against a live real-provider backend.

The runner is intentionally outside pytest: it is for long-form demo and
provider-hardening passes where every prompt, tool call, artifact, child
session, and caveat should be captured as evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.create_benchmark_data import create_benchmark_data

_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES = ("guard", "user_agent_keyword", "recovery")
_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH = 3
_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT = 2
_MARKETPLACE_COMPLEX_REQUIRED_CASES = 3
_SEMANTIC_REGRESSION_REQUIRED_PROOFS = {
    "no_shortcuts": "no deterministic or keyword-forced route sources",
    "root_delegation": "root Agent delegates through declared experts",
    "nested_tier3": "nested tier-3 or child-worker execution is observed",
    "sync_parent_return": "sync child delegation returns control to the parent",
    "failure_recovery": "failure/recovery behavior reaches downstream evidence",
    "workspace_memory_scope": "workspace/global memory scope policy is observed",
    "marketplace_pack": "marketplace Agent Blueprint activation is observed",
    "command_mcp_skill_scope": "command, MCP, or skill capability scoping is observed",
    "enabled_mcp_execution": "marketplace MCP descriptor is trusted, launched, and callable",
    "packaged_hook_invocation": "marketplace packaged hook is trusted, enabled, and invoked",
}
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
    ".las",
    ".laz",
    ".mzml",
    ".npy",
    ".npz",
    ".parquet",
    ".png",
    ".sac",
    ".tar",
    ".tgz",
    ".tsv",
    ".txt",
    ".vcf",
}
_CANONICAL_CASES_BY_ID: dict[str, "DemoCase"] | None = None


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
    agent_blueprint_id: str = ""
    min_expert_depth: int = 0
    min_branch_count: int = 0
    semantic_proofs: tuple[str, ...] = ()
    mcp_enable_descriptor_id: str = ""
    mcp_call_tool: str = ""
    mcp_call_args: dict[str, Any] = field(default_factory=dict)
    hook_enable_id: str = ""
    hook_probe_text: str = ""
    memory_scope_probe: bool = False
    skip_model_turn: bool = False


@dataclass
class DemoResult:
    """Recorded result for one demo case."""

    case: DemoCase
    session_id: str
    elapsed_s: float
    message: dict[str, Any]
    provider: dict[str, Any]
    child_sessions: list[dict[str, Any]] = field(default_factory=list)
    session_messages: list[dict[str, Any]] = field(default_factory=list)
    child_session_messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    setup_messages: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    benchmark_lane: str = "default"
    agent_blueprint: dict[str, Any] = field(default_factory=dict)
    semantic_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def selected_agent(self) -> str:
        """Return selected agent from the routing part, if present."""
        return _routing_agent(self.message) or _semantic_selected_agent(self.semantic_events)

    @property
    def text(self) -> str:
        """Return visible assistant text."""
        return _message_text(self.message)

    @property
    def visible_text(self) -> str:
        """Return only user-facing assistant text parts."""
        return _visible_message_text(self.message)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return tool call metadata."""
        tools = _tools(self.message)
        if tools:
            return tools
        return _semantic_tools(self.semantic_events)

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
        handoffs = _expert_handoffs(self.message)
        if handoffs:
            return handoffs
        return _semantic_handoffs(self.semantic_events)

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
    def active_agent_blueprint_id(self) -> str:
        """Return the session Agent Blueprint active for this benchmark case."""
        return str(self.agent_blueprint.get("active_agent_blueprint_id") or "")

    @property
    def expected_evidence_text(self) -> str:
        """Return text searched for expected benchmark evidence terms."""
        chunks: list[str] = [self.text, *self.artifacts]
        if self.agent_blueprint:
            chunks.append(json.dumps(self.agent_blueprint, sort_keys=True, default=str))
        if self.actions:
            chunks.append(json.dumps(self.actions, sort_keys=True, default=str))
        for tool in self.tools:
            chunks.append(str(tool.get("name") or tool.get("tool") or ""))
            for key in ("result", "args", "arguments", "params"):
                value = tool.get(key)
                if value is not None:
                    chunks.append(json.dumps(value, sort_keys=True, default=str))
        for handoff in self.expert_handoffs:
            for key in (
                "agent_id",
                "dispatch_target",
                "input_summary",
                "output_summary",
                "metadata",
            ):
                value = handoff.get(key)
                if value is not None:
                    chunks.append(json.dumps(value, sort_keys=True, default=str))
        return "\n".join(chunks)

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
            if (
                not self.case.agent_blueprint_id
                and self.selected_agent not in expected_agents
            ):
                return False
        if self.case.agent_blueprint_id:
            if self.active_agent_blueprint_id != self.case.agent_blueprint_id:
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
        lowered = self.expected_evidence_text.lower()
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
        if self.case.min_expert_depth and (
            self.route_metrics["expert_depth"] < self.case.min_expert_depth
        ):
            return False
        if self.case.min_branch_count and (
            self.route_metrics["branch_count"] < self.case.min_branch_count
        ):
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

    @property
    def semantic_event_types(self) -> list[str]:
        """Return semantic event types observed for this benchmark case."""
        return [str(row.get("event_type") or "") for row in self.semantic_events]

    @property
    def semantic_trace_summary(self) -> dict[str, Any]:
        """Return compact semantic trace proof for machine-readable evidence rows."""
        event_types = [event_type for event_type in self.semantic_event_types if event_type]
        trace_ids = sorted(
            {
                str(row.get("trace_id") or "")
                for row in self.semantic_events
                if row.get("trace_id")
            }
        )
        turn_ids = sorted(
            {
                str(row.get("turn_id") or "")
                for row in self.semantic_events
                if row.get("turn_id")
            }
        )
        live_count = sum(1 for row in self.semantic_events if row.get("live_observed") is True)
        return {
            "event_count": len(self.semantic_events),
            "live_event_count": live_count,
            "event_types": event_types,
            "unique_event_types": sorted(set(event_types)),
            "trace_ids": trace_ids,
            "turn_ids": turn_ids,
            "has_live_trace": bool(self.semantic_events) and live_count == len(self.semantic_events),
        }


def _message_text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text", "")) for part in message.get("parts", []))


def _visible_message_text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in message.get("parts", [])
        if part.get("type") == "text"
    )


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
    if not isinstance(rows, list):
        return []
    flattened: list[dict[str, Any]] = []

    def visit(row: Any) -> None:
        if not isinstance(row, dict):
            return
        flattened.append(row)
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return flattened


def _semantic_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _semantic_actor(row: dict[str, Any]) -> dict[str, Any]:
    actor = row.get("actor")
    return actor if isinstance(actor, dict) else {}


def _semantic_subject(row: dict[str, Any]) -> dict[str, Any]:
    subject = row.get("subject")
    return subject if isinstance(subject, dict) else {}


def _semantic_selected_agent(events: list[dict[str, Any]]) -> str:
    """Return the invoked root agent from semantic events, if available."""

    for row in events:
        if row.get("event_type") != "agent.invocation.started":
            continue
        agent_id = str(_semantic_actor(row).get("agent_id") or "")
        if agent_id:
            return agent_id
    return ""


def _semantic_tools(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover tool-call rows from semantic events for failed partial turns."""

    by_call_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    anonymous_index = 0
    for row in events:
        if str(row.get("event_type") or "") not in {
            "tool.call.started",
            "tool.call.completed",
        }:
            continue
        payload = _semantic_payload(row)
        actor = _semantic_actor(row)
        subject = _semantic_subject(row)
        tool = str(payload.get("tool") or actor.get("tool") or "")
        if not tool:
            continue
        call_id = str(payload.get("call_id") or subject.get("call_id") or "")
        if not call_id:
            anonymous_index += 1
            call_id = f"semantic_tool_{anonymous_index}"
        if call_id not in by_call_id:
            order.append(call_id)
            by_call_id[call_id] = {
                "name": tool,
                "tool": tool,
                "call_id": call_id,
                "telemetry_source": "semantic_event",
            }
        target = by_call_id[call_id]
        if row.get("event_type") == "tool.call.completed":
            target["ok"] = bool(payload.get("ok", row.get("status") != "failed"))
            if payload.get("duration_ms") is not None:
                target["duration_ms"] = payload.get("duration_ms")
            if payload.get("cached") is not None:
                target["cached"] = payload.get("cached")
        elif "ok" not in target:
            target["ok"] = False
            target["status"] = "started"
    return [by_call_id[call_id] for call_id in order]


def _semantic_handoffs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover delegation provenance rows from semantic events."""

    handoffs: list[dict[str, Any]] = []
    for row in events:
        event_type = str(row.get("event_type") or "")
        if not event_type.startswith("delegation."):
            continue
        payload = _semantic_payload(row)
        actor = _semantic_actor(row)
        subject = _semantic_subject(row)
        if event_type == "delegation.started":
            agent_id = str(
                payload.get("agent_id")
                or payload.get("delegate_to")
                or subject.get("agent_id")
                or ""
            )
            parent_id = str(payload.get("parent_id") or actor.get("agent_id") or "")
            stage = str(payload.get("stage") or "delegate.started")
        elif event_type == "delegation.completed":
            agent_id = str(payload.get("agent_id") or actor.get("agent_id") or "")
            parent_id = str(payload.get("parent_id") or subject.get("agent_id") or "")
            stage = str(payload.get("stage") or "delegate.completed")
        elif event_type == "delegation.parent_resumed":
            agent_id = str(payload.get("agent_id") or actor.get("agent_id") or "")
            parent_id = str(payload.get("parent_id") or "")
            stage = str(payload.get("stage") or "parent.resumed")
        else:
            continue
        if not agent_id:
            continue
        handoffs.append(
            {
                "agent_id": agent_id,
                "parent_id": parent_id,
                "return_to": parent_id if stage in {"delegate.completed", "delegate.failed"} else "",
                "stage": stage,
                "status": str(payload.get("status") or row.get("status") or ""),
                "delegation_lifecycle": str(payload.get("delegation_lifecycle") or "sync"),
                "dispatch_target": str(payload.get("dispatch_target") or agent_id),
                "resumed_from": str(payload.get("resumed_from") or subject.get("agent_id") or ""),
                "telemetry_source": "semantic_event",
            }
        )
    return handoffs


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
        path = _clean_path_candidate(candidate)
        if not path or path.startswith("//") or re.match(r"^[a-z]+://", path, re.I):
            continue
        if path not in deduped:
            deduped.append(path)
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
    """Build a machine-readable route graph from parent-owned handoff evidence."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    edge_keys: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, node_type: str) -> None:
        if not node_id:
            return
        if any(row["id"] == node_id and row["type"] == node_type for row in nodes):
            return
        nodes.append({"id": node_id, "type": node_type})

    def add_edge(source: str, target: str, kind: str) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, kind)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({"from": source, "to": target, "kind": kind})

    add_node("orchestrator", "orchestrator")

    saw_root_handoff = False
    previous_expert = ""
    for row in result.expert_handoffs:
        agent_id = str(row.get("agent_id") or "")
        if not agent_id:
            continue
        parent_id = str(row.get("parent_id") or "")
        stage = str(row.get("stage") or "")
        add_node(agent_id, "expert")
        if stage.startswith("delegate."):
            if parent_id:
                add_node(parent_id, "expert")
                add_edge(parent_id, agent_id, "handoff")
                add_edge(agent_id, parent_id, "return")
        elif stage == "parent.resumed":
            continue
        elif parent_id:
            add_node(parent_id, "expert")
            add_edge(parent_id, agent_id, "handoff")
            if stage == "direct_tool":
                add_edge(agent_id, parent_id, "return")
        elif previous_expert and stage != "planner_dispatch":
            add_edge(previous_expert, agent_id, "handoff")
        else:
            saw_root_handoff = True
            add_edge("orchestrator", agent_id, "route")
        previous_expert = agent_id

    selected = result.selected_agent
    if selected and not saw_root_handoff:
        add_node(selected, "expert")
        add_edge("orchestrator", selected, "route")

    for child in result.child_sessions:
        child_id = str(child.get("id") or child.get("title") or child.get("name") or "")
        if not child_id:
            continue
        add_node(child_id, "child_session")
        add_edge(selected or "orchestrator", child_id, "branch")

    return {"nodes": nodes, "edges": edges}


def _route_metrics(result: DemoResult) -> dict[str, Any]:
    """Return aggregate routing/tool metrics for benchmark comparison."""
    graph = _route_graph(result)
    expert_nodes = [row for row in graph["nodes"] if row.get("type") == "expert"]
    child_session_branch_edges = [
        row for row in graph["edges"] if row.get("kind") == "branch"
    ]
    sync_handoff_pairs: set[tuple[str, str]] = set()
    for row in graph["edges"]:
        kind = row.get("kind")
        source = str(row.get("from") or "")
        target = str(row.get("to") or "")
        if not source or not target:
            continue
        if kind == "handoff":
            sync_handoff_pairs.add((source, target))
        elif kind == "return":
            sync_handoff_pairs.add((target, source))
    return {
        "expert_depth": len(expert_nodes),
        "branch_count": len(child_session_branch_edges) + len(sync_handoff_pairs),
        "child_session_branch_count": len(child_session_branch_edges),
        "sync_handoff_count": len(sync_handoff_pairs),
        "unique_experts": len({row.get("id") for row in expert_nodes}),
        "unique_tools": len(set(result.tool_names)),
        "tool_call_count": len(result.tools),
        "artifact_count": len(result.artifacts),
    }


def _handoff_field(row: dict[str, Any], key: str) -> str:
    """Return a handoff field, checking row metadata for runtime variants."""
    value = row.get(key)
    if value:
        return str(value)
    metadata = row.get("metadata") or {}
    if isinstance(metadata, dict):
        return str(metadata.get(key) or "")
    return ""


def _sync_delegation_pairs(result: DemoResult) -> list[tuple[str, str]]:
    """Return parent/child pairs that claim sync delegation provenance."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in result.expert_handoffs:
        child_id = str(row.get("agent_id") or "")
        parent_id = str(row.get("parent_id") or "")
        if not child_id or not parent_id or child_id == parent_id:
            continue
        stage = str(row.get("stage") or "")
        lifecycle = _handoff_field(row, "delegation_lifecycle")
        if (
            stage.endswith("_child")
            or stage.startswith("delegate.")
            or (
                lifecycle == "sync"
                and stage not in {"parent.resumed", "delegate.completed", "delegate.failed"}
            )
        ):
            pair = (parent_id, child_id)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def _missing_sync_return_pairs(result: DemoResult) -> list[str]:
    """Return delegated child pairs missing child-return or parent-resume evidence."""
    missing: list[str] = []
    for parent_id, child_id in _sync_delegation_pairs(result):
        has_child_return = False
        has_parent_resume = False
        for row in result.expert_handoffs:
            stage = str(row.get("stage") or "")
            agent_id = str(row.get("agent_id") or "")
            row_parent = str(row.get("parent_id") or "")
            if (
                agent_id == child_id
                and row_parent == parent_id
                and stage == "direct_tool"
                and str(row.get("status") or "") in {"success", "failure"}
            ):
                has_child_return = True
                has_parent_resume = True
            if (
                agent_id == child_id
                and row_parent == parent_id
                and stage in {"delegate.completed", "delegate.failed"}
                and _handoff_field(row, "return_to") == parent_id
            ):
                has_child_return = True
            if (
                agent_id == parent_id
                and stage == "parent.resumed"
                and _handoff_field(row, "resumed_from") == child_id
            ):
                has_parent_resume = True
        if not has_child_return or not has_parent_resume:
            missing.append(f"{parent_id}->{child_id}")
    return missing


def _meets_complex_hierarchy_threshold(
    result: DemoResult,
    *,
    min_expert_depth: int = _MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
    min_branch_count: int = _MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
) -> bool:
    """Return whether a benchmark row proves a non-shallow hierarchy."""

    metrics = result.route_metrics
    return (
        result.passed
        and metrics["expert_depth"] >= min_expert_depth
        and metrics["branch_count"] >= min_branch_count
        and metrics["sync_handoff_count"] >= min_branch_count
        and not _missing_sync_return_pairs(result)
    )


def _case_observed_semantic_proofs(result: DemoResult) -> tuple[str, ...]:
    """Return declared semantic proofs supported by observed result evidence."""

    if not result.case.semantic_proofs:
        return ()
    observed: list[str] = []
    route_ok = result.route_source not in result.case.forbidden_route_sources
    for proof in result.case.semantic_proofs:
        proof_observed = False
        if proof == "no_shortcuts":
            proof_observed = route_ok
        elif not result.passed:
            proof_observed = False
        elif proof == "root_delegation":
            proof_observed = route_ok and result.route_metrics["expert_depth"] > 0
        elif proof == "nested_tier3":
            proof_observed = bool(result.child_sessions) or result.route_metrics[
                "expert_depth"
            ] >= 3
        elif proof == "sync_parent_return":
            proof_observed = (
                result.route_metrics["sync_handoff_count"] > 0
                and not _missing_sync_return_pairs(result)
            )
        elif proof == "failure_recovery":
            proof_observed = (
                any(name.startswith("ndp_") for name in result.tool_names)
                and any(name.startswith("sac_") for name in result.tool_names)
                and bool(result.artifact_evidence)
                and all(
                    row.get("exists") and int(row.get("size_bytes") or 0) > 0
                    for row in result.artifact_evidence
                )
            )
        elif proof == "workspace_memory_scope":
            proof_observed = _workspace_memory_scope_observed(result)
        elif proof == "marketplace_pack":
            proof_observed = (
                bool(result.case.agent_blueprint_id)
                and result.active_agent_blueprint_id == result.case.agent_blueprint_id
                and bool(result.tool_names or result.handoff_agent_ids)
            )
        elif proof == "command_mcp_skill_scope":
            proof_observed = _command_mcp_skill_scope_observed(result)
        elif proof == "enabled_mcp_execution":
            proof_observed = _enabled_mcp_execution_observed(result)
        elif proof == "packaged_hook_invocation":
            proof_observed = _packaged_hook_invocation_observed(result)
        if proof_observed:
            observed.append(proof)
    return tuple(observed)


def _command_mcp_skill_scope_observed(result: DemoResult) -> bool:
    """Return whether session evidence proves command/MCP/skill capability scoping."""

    blueprint_text = json.dumps(result.agent_blueprint, sort_keys=True, default=str).lower()
    runtime_text = ""
    metadata = result.message.get("metadata") if isinstance(result.message, Mapping) else {}
    if isinstance(metadata, Mapping):
        runtime_text = json.dumps(
            metadata.get("runtime_provenance") or {},
            sort_keys=True,
            default=str,
        ).lower()
    session_text = json.dumps(
        result.session_messages,
        sort_keys=True,
        default=str,
    ).lower()
    tool_text = json.dumps(result.tools, sort_keys=True, default=str).lower()
    handoff_text = json.dumps(result.expert_handoffs, sort_keys=True, default=str).lower()
    combined = "\n".join([blueprint_text, runtime_text, session_text, tool_text, handoff_text])

    has_declared_surface = any(
        marker in combined
        for marker in (
            "mcp_descriptors",
            "agent_blueprint_mcp_descriptor",
            '"commands"',
            '"skills"',
            '"resolved_skills"',
            "capability_refs",
        )
    )
    has_scope_status = any(
        marker in combined
        for marker in (
            "requires explicit enablement",
            "explicit trust",
            '"enabled": false',
            '"enabled": true',
            "disabled",
            "declared",
            "resolved",
        )
    )
    return result.passed and has_declared_surface and has_scope_status


def _enabled_mcp_execution_observed(result: DemoResult) -> bool:
    """Return whether actions prove enabled marketplace MCP launch and call."""

    enable_actions = [
        action
        for action in result.actions
        if action.get("type") == "agent_blueprint_mcp_enable" and action.get("ok") is True
    ]
    call_actions = [
        action
        for action in result.actions
        if action.get("type") == "mcp_tool_call" and action.get("ok") is True
    ]
    if not result.passed or not enable_actions or not call_actions:
        return False
    ready_tools = {
        tool
        for action in enable_actions
        for tool in action.get("ready_tools", [])
        if isinstance(tool, str)
    }
    called_tools = {
        str(action.get("tool") or "")
        for action in call_actions
        if str(action.get("tool") or "")
    }
    trusted = any(
        isinstance(action.get("trust"), Mapping)
        and (action.get("trust") or {}).get("trusted") is True
        for action in enable_actions
    )
    return bool(ready_tools.intersection(called_tools)) and trusted


def _workspace_memory_scope_observed(result: DemoResult) -> bool:
    """Return whether structured actions prove memory/workspace policy scope."""

    action = next(
        (
            row
            for row in result.actions
            if row.get("type") == "workspace_memory_scope_probe" and row.get("ok") is True
        ),
        None,
    )
    if action is not None:
        decisions = {
            str(row.get("policy_decision") or row.get("decision") or "")
            for row in action.get("checks") or []
            if isinstance(row, Mapping)
        }
        statuses = {
            str(row.get("name") or ""): bool(row.get("ok"))
            for row in action.get("checks") or []
            if isinstance(row, Mapping)
        }
        return (
            result.passed
            and statuses.get("deny_without_intent") is True
            and statuses.get("allow_same_workspace_with_intent") is True
            and statuses.get("deny_other_workspace_summary") is True
            and "deny_cross_session_requires_intent" in decisions
            and "allow_same_workspace_user_intent" in decisions
            and "deny_other_workspace" in decisions
            and action.get("same_workspace_hit_session_id") == action.get("prior_session_id")
            and action.get("other_workspace_hit_session_id") in {"", None}
        )
    evidence = result.expected_evidence_text.lower()
    return result.passed and (
        "workspace_scope" in evidence
        or "policy_decision" in evidence
        or "memory_scope" in evidence
    )


def _packaged_hook_invocation_observed(result: DemoResult) -> bool:
    """Return whether actions and semantic events prove packaged hook invocation."""

    enable_actions = [
        action
        for action in result.actions
        if action.get("type") == "agent_blueprint_hook_enable" and action.get("ok") is True
    ]
    probe_actions = [
        action
        for action in result.actions
        if action.get("type") == "packaged_hook_probe" and action.get("ok") is True
    ]
    if not result.passed or not enable_actions or not probe_actions:
        return False
    hook_ids = {
        str(action.get("hook_id") or "")
        for action in enable_actions
        if str(action.get("hook_id") or "")
    }
    trusted = any(
        isinstance(action.get("trust"), Mapping)
        and (action.get("trust") or {}).get("trusted") is True
        for action in enable_actions
    )
    if not hook_ids or not trusted:
        return False
    events = list(result.semantic_events)
    for action in probe_actions:
        for event in action.get("semantic_events") or []:
            if isinstance(event, Mapping):
                events.append(dict(event))
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in {"hook.invocation.completed", "hook.pre_message.blocked"}:
            continue
        actor = event.get("actor") if isinstance(event.get("actor"), Mapping) else {}
        if str(actor.get("hook") or "") not in hook_ids:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        handlers = payload.get("handlers") if isinstance(payload.get("handlers"), list) else []
        for handler in handlers:
            if not isinstance(handler, Mapping):
                continue
            if (
                handler.get("source") == "agent_blueprint"
                and handler.get("agent_blueprint_id") == result.case.agent_blueprint_id
                and str(handler.get("definition_path") or "").endswith(
                    f"hooks/{actor.get('hook')}.py"
                )
            ):
                return True
    return False


def _children(http: httpx.Client, parent_session_id: str) -> list[dict[str, Any]]:
    sessions = http.get("/v1/sessions").json()["sessions"]
    return [row for row in sessions if row.get("parent_session_id") == parent_session_id]


def _session_messages(http: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return the full persisted GACT message log for a session."""

    return http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]


def _chronological_session_messages(http: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return session messages sorted oldest-first for human audit logs."""

    messages = _session_messages(http, session_id)
    return sorted(messages, key=lambda message: str(message.get("created_at") or ""))


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
        messages = _session_messages(http, session_id)
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


def _semantic_events_for_completed_message(
    http: httpx.Client,
    session_id: str,
    assistant_message_id: str,
    *,
    timeout_s: float = 20.0,
) -> list[dict[str, Any]]:
    """Replay SSE history and return semantic events for one completed turn."""

    if not assistant_message_id:
        return []
    current_turn_events: list[dict[str, Any]] = []
    try:
        with httpx.stream(
            "GET",
            f"{http.base_url}/v1/sessions/{session_id}/events",
            timeout=timeout_s,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    env = json.loads(line[len("data: ") :])
                except json.JSONDecodeError:
                    continue
                event_type = str(env.get("type") or "")
                payload = env.get("payload")
                if not isinstance(payload, dict):
                    continue
                if event_type == "semantic.event":
                    current_turn_events.append(payload)
                    continue
                if event_type != "message.completed":
                    continue
                if str(payload.get("message_id") or "") == assistant_message_id:
                    return current_turn_events
                # This completed an older replayed turn. Keep only events after it.
                current_turn_events = []
    except Exception:
        return []
    return []


def _semantic_events_snapshot(
    http: httpx.Client,
    session_id: str,
    *,
    timeout_s: float = 2.0,
) -> list[dict[str, Any]]:
    """Return currently replayable semantic SSE events for a session."""

    events: list[dict[str, Any]] = []
    try:
        with httpx.stream(
            "GET",
            f"{http.base_url}/v1/sessions/{session_id}/events",
            timeout=timeout_s,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    env = json.loads(line[len("data: ") :])
                except json.JSONDecodeError:
                    continue
                if env.get("type") != "semantic.event":
                    continue
                payload = env.get("payload")
                if isinstance(payload, dict):
                    events.append(payload)
    except Exception:
        return events
    return events


def _parse_event_time(value: object) -> datetime | None:
    """Parse a GACT event timestamp, returning None for malformed rows."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_event_agent(payload: dict[str, Any]) -> str:
    """Return the most useful agent/expert label in a semantic payload."""

    actor = payload.get("actor") if isinstance(payload.get("actor"), dict) else {}
    subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    for row in (actor, subject, payload):
        for key in ("agent_id", "agent", "expert_id", "tool", "message_id"):
            value = row.get(key) if isinstance(row, dict) else None
            if isinstance(value, str) and value:
                return value
    return ""


def _format_live_event_line(envelope: dict[str, Any]) -> str:
    """Format one SSE envelope as compact operator-facing benchmark output."""

    event_type = str(envelope.get("type") or "")
    payload = envelope.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event_type in {"server.connected", "server.heartbeat"}:
        return ""
    if event_type == "semantic.event":
        semantic_type = str(payload.get("event_type") or "semantic.event")
        status = str(payload.get("status") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        agent = _compact_event_agent(payload)
        bits = [f"semantic {semantic_type}"]
        if agent:
            bits.append(f"agent={agent}")
        if status and status != "completed":
            bits.append(f"status={status}")
        if summary:
            bits.append(summary)
        return " | ".join(bits)
    if event_type.startswith("message."):
        message_id = str(payload.get("message_id") or payload.get("id") or "")
        status = str(payload.get("status") or payload.get("stop_reason") or "")
        bits = [event_type]
        if message_id:
            bits.append(f"message={message_id}")
        if status:
            bits.append(f"status={status}")
        return " | ".join(bits)
    if event_type.startswith("tool.call."):
        tool = str(payload.get("tool") or payload.get("name") or "")
        ok = payload.get("ok")
        bits = [event_type]
        if tool:
            bits.append(f"tool={tool}")
        if ok is not None:
            bits.append(f"ok={bool(ok)}")
        return " | ".join(bits)
    if event_type.startswith(("lm.provider.", "mcp.server.", "session.")):
        summary = str(payload.get("message") or payload.get("status") or "").strip()
        return f"{event_type}{' | ' + summary if summary else ''}"
    if event_type.endswith(".failed") or event_type.endswith(".error"):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        return f"{event_type}{' | ' + message if message else ''}"
    return ""


class _LiveEventWatch:
    """Background SSE watcher used only for manual benchmark operation."""

    def __init__(
        self,
        base_url: str,
        session_id: str,
        *,
        enabled: bool,
        prefix: str = "    live",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.enabled = enabled
        self.prefix = prefix
        self.started_at = datetime.now(timezone.utc)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LiveEventWatch":
        if not self.enabled:
            return self
        self._thread = threading.Thread(target=self._run, name="clio-benchmark-sse-watch")
        self._thread.daemon = True
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            with httpx.stream(
                "GET",
                f"{self.base_url}/v1/sessions/{self.session_id}/events",
                timeout=None,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if not line.startswith("data: "):
                        continue
                    try:
                        envelope = json.loads(line[len("data: ") :])
                    except json.JSONDecodeError:
                        continue
                    event_time = _parse_event_time(envelope.get("occurred_at"))
                    if event_time is not None and event_time < self.started_at:
                        continue
                    formatted = _format_live_event_line(envelope)
                    if formatted:
                        print(f"{self.prefix} {formatted}", flush=True)
                    if str(envelope.get("type") or "") == "message.completed":
                        return
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                print(f"{self.prefix} watch_error | {type(exc).__name__}: {exc}", flush=True)


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


def _enable_blueprint_mcp_for_case(
    http: httpx.Client,
    case: DemoCase,
    *,
    workspace_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Trust/enable an Agent Blueprint MCP descriptor for benchmark evidence."""

    started = time.monotonic()
    descriptor_id = case.mcp_enable_descriptor_id
    if not case.agent_blueprint_id or not descriptor_id:
        return {
            "type": "agent_blueprint_mcp_enable",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": "case is missing agent_blueprint_id or mcp_enable_descriptor_id",
        }
    try:
        response = http.post(
            f"/v1/agent-blueprints/{case.agent_blueprint_id}/mcp/{descriptor_id}/enable",
            json={"workspace_id": workspace_id, "trust": True, "probe": True},
            timeout=timeout_s,
        )
        payload = response.json()
        tools = payload.get("tools") if isinstance(payload, Mapping) else []
        ready_tools = [
            str(tool.get("name") or tool.get("id") or "")
            for tool in tools
            if isinstance(tool, Mapping) and tool.get("enabled") is True
        ]
        return {
            "type": "agent_blueprint_mcp_enable",
            "ok": response.status_code == 200 and bool(ready_tools),
            "status_code": response.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "descriptor_id": descriptor_id,
            "server_id": payload.get("id") if isinstance(payload, Mapping) else "",
            "status": payload.get("status") if isinstance(payload, Mapping) else "",
            "ready_tools": ready_tools,
            "trust": payload.get("trust") if isinstance(payload, Mapping) else {},
            "result": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "agent_blueprint_mcp_enable",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "descriptor_id": descriptor_id,
            "error": repr(exc),
        }


def _enable_blueprint_hook_for_case(
    http: httpx.Client,
    case: DemoCase,
    *,
    workspace_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Trust/enable an Agent Blueprint packaged hook for benchmark evidence."""

    started = time.monotonic()
    hook_id = case.hook_enable_id
    if not case.agent_blueprint_id or not hook_id:
        return {
            "type": "agent_blueprint_hook_enable",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": "case is missing agent_blueprint_id or hook_enable_id",
        }
    try:
        response = http.post(
            f"/v1/agent-blueprints/{case.agent_blueprint_id}/hooks/{hook_id}/enable",
            json={"workspace_id": workspace_id, "trust": True},
            timeout=timeout_s,
        )
        payload = response.json()
        return {
            "type": "agent_blueprint_hook_enable",
            "ok": response.status_code == 200 and payload.get("status") == "enabled",
            "status_code": response.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "hook_id": hook_id,
            "status": payload.get("status") if isinstance(payload, Mapping) else "",
            "installed_path": payload.get("installed_path") if isinstance(payload, Mapping) else "",
            "checksum": payload.get("checksum") if isinstance(payload, Mapping) else "",
            "trust": payload.get("trust") if isinstance(payload, Mapping) else {},
            "result": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "agent_blueprint_hook_enable",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "hook_id": hook_id,
            "error": repr(exc),
        }


def _probe_packaged_hook_for_case(
    http: httpx.Client,
    case: DemoCase,
    *,
    session_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Send a benchmark probe message that should invoke a packaged hook."""

    started = time.monotonic()
    prompt = case.hook_probe_text or case.prompt
    if not prompt:
        return {
            "type": "packaged_hook_probe",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": "case is missing hook_probe_text and prompt",
        }
    try:
        response = http.post(
            f"/v1/sessions/{session_id}/messages",
            json={"parts": [{"type": "text", "text": prompt}]},
            timeout=timeout_s,
        )
        payload = response.json()
        deadline = time.monotonic() + min(timeout_s, 10.0)
        session: dict[str, Any] = {}
        while time.monotonic() < deadline:
            session = http.get(f"/v1/sessions/{session_id}", timeout=timeout_s).json()
            if session.get("status") == "error":
                break
            time.sleep(0.1)
        semantic_events = _semantic_events_snapshot(http, session_id)
        blocked = any(
            event.get("event_type") == "hook.pre_message.blocked"
            for event in semantic_events
            if isinstance(event, Mapping)
        )
        return {
            "type": "packaged_hook_probe",
            "ok": response.status_code == 200 and blocked,
            "status_code": response.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "hook_id": case.hook_enable_id,
            "message_id": payload.get("message_id") if isinstance(payload, Mapping) else "",
            "session_status": session.get("status") if isinstance(session, Mapping) else "",
            "semantic_events": semantic_events,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "packaged_hook_probe",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "agent_blueprint_id": case.agent_blueprint_id,
            "hook_id": case.hook_enable_id,
            "error": repr(exc),
        }


def _create_benchmark_workspace(
    http: httpx.Client,
    *,
    name: str,
    root_path: str,
    timeout_s: float,
) -> str:
    """Create a workspace for benchmark setup actions and return its id."""

    response = http.post(
        "/v1/workspaces",
        json={"name": name, "root_path": root_path},
        timeout=timeout_s,
    )
    response.raise_for_status()
    return str(response.json().get("id") or "")


def _create_benchmark_session(
    http: httpx.Client,
    *,
    title: str,
    workspace_id: str,
    timeout_s: float,
) -> str:
    """Create a session for benchmark setup actions and return its id."""

    response = http.post(
        "/v1/sessions",
        json={"title": title, **({"workspace_id": workspace_id} if workspace_id else {})},
        timeout=timeout_s,
    )
    response.raise_for_status()
    return str(response.json().get("id") or "")


def _memory_policy_detail(response: httpx.Response) -> str:
    """Return the most useful memory policy decision from a response."""

    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    if metadata.get("policy_decision"):
        return str(metadata.get("policy_decision") or "")
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    details = error.get("details") if isinstance(error.get("details"), Mapping) else {}
    return str(details.get("policy_decision") or details.get("scope") or "")


def _memory_hit_session_ids(payload: Mapping[str, Any]) -> list[str]:
    """Return hit session ids from a memory search response payload."""

    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    return [
        str(hit.get("session_id") or "")
        for hit in hits
        if isinstance(hit, Mapping) and str(hit.get("session_id") or "")
    ]


def _probe_workspace_memory_scope_for_case(
    http: httpx.Client,
    case: DemoCase,
    *,
    session_id: str,
    workspace_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Exercise memory tool scope policy through live GACT endpoints."""

    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    try:
        other_workspace_id = _create_benchmark_workspace(
            http,
            name=f"memory probe other {case.case_id}",
            root_path=str(Path.cwd() / "tmp" / f"{case.case_id}-other-workspace"),
            timeout_s=timeout_s,
        )
        prior_session_id = _create_benchmark_session(
            http,
            title=f"memory prior {case.case_id}",
            workspace_id=workspace_id,
            timeout_s=timeout_s,
        )
        other_session_id = _create_benchmark_session(
            http,
            title=f"memory other {case.case_id}",
            workspace_id=other_workspace_id,
            timeout_s=timeout_s,
        )
        marker_id = uuid.uuid4().hex[:12]
        prior_marker = f"ALPHA_WORKSPACE_MEMORY_{case.case_id}_{marker_id}"
        other_marker = f"BETA_OTHER_WORKSPACE_MEMORY_{case.case_id}_{marker_id}"
        query = f"WORKSPACE_MEMORY_{case.case_id}_{marker_id}"
        _post_turn(
            http,
            prior_session_id,
            (
                f"Dataset memory seed {prior_marker}: pressure dataset belongs to "
                "the current workspace and may be reused only with explicit user intent."
            ),
            timeout_s=timeout_s,
        )
        _post_turn(
            http,
            other_session_id,
            (
                f"Dataset memory seed {other_marker}: pressure dataset belongs to "
                "a different workspace and must not leak into the current workspace."
            ),
            timeout_s=timeout_s,
        )

        denied = http.post(
            f"/v1/sessions/{session_id}/memory/tools/search-sessions",
            json={"query": query, "scope": "current_workspace", "limit": 10},
            timeout=timeout_s,
        )
        denied_decision = _memory_policy_detail(denied)
        checks.append(
            {
                "name": "deny_without_intent",
                "ok": denied.status_code == 403
                and denied_decision == "deny_cross_session_requires_intent",
                "status_code": denied.status_code,
                "policy_decision": denied_decision,
            }
        )

        allowed = http.post(
            f"/v1/sessions/{session_id}/memory/tools/search-sessions",
            json={
                "query": query,
                "scope": "current_workspace",
                "user_intent": "answer the user's request about work from the last few days",
                "caller": {"type": "agent", "agent_id": "orchestrator"},
                "limit": 10,
            },
            timeout=timeout_s,
        )
        allowed_payload = allowed.json()
        allowed_payload = allowed_payload if isinstance(allowed_payload, Mapping) else {}
        allowed_decision = _memory_policy_detail(allowed)
        hit_session_ids = _memory_hit_session_ids(allowed_payload)
        checks.append(
            {
                "name": "allow_same_workspace_with_intent",
                "ok": allowed.status_code == 200
                and allowed_decision == "allow_same_workspace_user_intent"
                and prior_session_id in hit_session_ids
                and other_session_id not in hit_session_ids,
                "status_code": allowed.status_code,
                "policy_decision": allowed_decision,
                "hit_session_ids": hit_session_ids,
            }
        )

        other_denied = http.post(
            f"/v1/sessions/{session_id}/memory/tools/read-session-summary",
            json={
                "target_session_id": other_session_id,
                "scope": "current_workspace",
                "user_intent": "look across my recent work",
            },
            timeout=timeout_s,
        )
        other_decision = _memory_policy_detail(other_denied)
        checks.append(
            {
                "name": "deny_other_workspace_summary",
                "ok": other_denied.status_code == 403 and other_decision == "deny_other_workspace",
                "status_code": other_denied.status_code,
                "decision": other_decision,
            }
        )

        same_workspace_hit = prior_session_id if prior_session_id in hit_session_ids else ""
        other_workspace_hit = other_session_id if other_session_id in hit_session_ids else ""
        return {
            "type": "workspace_memory_scope_probe",
            "ok": all(row.get("ok") is True for row in checks),
            "elapsed_s": round(time.monotonic() - started, 3),
            "workspace_id": workspace_id,
            "other_workspace_id": other_workspace_id,
            "current_session_id": session_id,
            "prior_session_id": prior_session_id,
            "other_session_id": other_session_id,
            "same_workspace_hit_session_id": same_workspace_hit,
            "other_workspace_hit_session_id": other_workspace_hit,
            "query": query,
            "checks": checks,
            "provenance": {
                "source": "gact_memory_tool",
                "tool_name": "memory_search_sessions",
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "workspace_memory_scope_probe",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "workspace_id": workspace_id,
            "current_session_id": session_id,
            "checks": checks,
            "error": repr(exc),
        }


def _call_enabled_mcp_for_case(
    http: httpx.Client,
    case: DemoCase,
    enable_action: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Call an enabled MCP tool through CLIO and record structured evidence."""

    started = time.monotonic()
    server_id = str(enable_action.get("server_id") or "")
    if not server_id or not case.mcp_call_tool:
        return {
            "type": "mcp_tool_call",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "error": "case is missing enabled server_id or mcp_call_tool",
        }
    try:
        response = http.post(
            f"/v1/mcp/servers/{server_id}/call",
            json={"tool": case.mcp_call_tool, "args": case.mcp_call_args},
            timeout=timeout_s,
        )
        payload = response.json()
        return {
            "type": "mcp_tool_call",
            "ok": response.status_code == 200 and payload.get("is_error") is not True,
            "status_code": response.status_code,
            "elapsed_s": round(time.monotonic() - started, 3),
            "server_id": server_id,
            "tool": case.mcp_call_tool,
            "args": case.mcp_call_args,
            "result": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "type": "mcp_tool_call",
            "ok": False,
            "elapsed_s": round(time.monotonic() - started, 3),
            "server_id": server_id,
            "tool": case.mcp_call_tool,
            "args": case.mcp_call_args,
            "error": repr(exc),
        }


def _action_only_message(case: DemoCase, actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a benchmark-only message for cases proven by setup actions."""

    tool_calls: list[dict[str, Any]] = []
    for action in actions:
        if action.get("type") != "mcp_tool_call" or not action.get("tool"):
            continue
        tool_calls.append(
            {
                "name": str(action.get("tool") or ""),
                "tool": str(action.get("tool") or ""),
                "args": action.get("args") or {},
                "result": action.get("result") or {},
                "ok": action.get("ok") is True,
                "telemetry_source": "benchmark_action",
            }
        )
    return {
        "id": f"benchmark_action_{case.case_id}",
        "role": "assistant",
        "parts": [],
        "metadata": {
            "benchmark_action_only": True,
            "tools_called": tool_calls,
            "stream_source": "action_only",
        },
        "error_info": None,
        "stop_reason": "end_turn",
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
        "session_log": {
            "root_session_id": result.session_id,
            "root_messages": result.session_messages,
            "child_sessions": [
                {
                    "session_id": session_id,
                    "session": next(
                        (
                            child
                            for child in result.child_sessions
                            if str(child.get("id") or "") == session_id
                        ),
                        {},
                    ),
                    "messages": messages,
                }
                for session_id, messages in result.child_session_messages.items()
            ],
        },
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
        "agent_blueprint": result.agent_blueprint,
        "semantic_trace": result.semantic_trace_summary,
        "semantic_events": result.semantic_events,
        "stream_source": result.stream_source,
        "stream_fallback": result.stream_fallback,
        "routing_mode": result.case.routing_mode,
        "forbidden_route_sources": list(result.case.forbidden_route_sources),
        "min_expert_depth": result.case.min_expert_depth,
        "min_branch_count": result.case.min_branch_count,
        "semantic_proofs": list(result.case.semantic_proofs),
        "observed_semantic_proofs": list(_case_observed_semantic_proofs(result)),
        "benchmark_lane": result.benchmark_lane,
        "complexity_score": result.complexity_score,
        "answer_excerpt": result.visible_text[:1200],
        "complexity_tags": list(result.case.complexity_tags),
    }


def _result_from_case_row(row: dict[str, Any]) -> DemoResult:
    """Rehydrate a recorded JSONL evidence row for markdown report rendering."""
    case_id = str(row.get("case") or "")
    canonical_case = _canonical_cases_by_id().get(case_id)
    if canonical_case is not None:
        case = replace(
            canonical_case,
            title=str(row.get("title") or canonical_case.title),
            category=str(row.get("category") or canonical_case.category),
            session_group=str(row.get("session_group") or canonical_case.session_group),
            prompt=str(row.get("prompt") or canonical_case.prompt),
            expected=str(row.get("expected") or canonical_case.expected),
            why=str(row.get("why") or canonical_case.why),
            routing_mode=str(row.get("routing_mode") or canonical_case.routing_mode),
            min_expert_depth=int(row.get("min_expert_depth") or canonical_case.min_expert_depth),
            min_branch_count=int(row.get("min_branch_count") or canonical_case.min_branch_count),
            semantic_proofs=tuple(
                str(proof) for proof in row.get("semantic_proofs") or canonical_case.semantic_proofs
            ),
        )
    else:
        case = DemoCase(
            case_id=case_id,
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
            agent_blueprint_id=str(
                (row.get("agent_blueprint") or {}).get("active_agent_blueprint_id")
                if isinstance(row.get("agent_blueprint"), dict)
                else ""
            ),
            min_expert_depth=int(row.get("min_expert_depth") or 0),
            min_branch_count=int(row.get("min_branch_count") or 0),
            semantic_proofs=tuple(str(proof) for proof in row.get("semantic_proofs", []) or []),
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
    session_log = row.get("session_log") if isinstance(row.get("session_log"), dict) else {}
    child_logs = {}
    for child in session_log.get("child_sessions", []) if isinstance(session_log, dict) else []:
        if not isinstance(child, dict):
            continue
        session_id = str(child.get("session_id") or "")
        if session_id:
            child_logs[session_id] = list(child.get("messages") or [])
    return DemoResult(
        case=case,
        session_id=str(row.get("session_id") or ""),
        elapsed_s=float(row.get("elapsed_s") or 0.0),
        message=message,
        provider=dict(row.get("provider") or {}),
        child_sessions=list(row.get("child_sessions") or []),
        session_messages=list(session_log.get("root_messages") or [])
        if isinstance(session_log, dict)
        else [],
        child_session_messages=child_logs,
        setup_messages=setup_messages,
        actions=list(row.get("actions") or []),
        benchmark_lane=str(row.get("benchmark_lane") or "recorded"),
        agent_blueprint=dict(row.get("agent_blueprint") or {}),
        semantic_events=list(row.get("semantic_events") or []),
    )


def _rows_from_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load benchmark evidence rows from a JSONL file."""

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _apply_render_lane(results: list[DemoResult], lane: str) -> list[DemoResult]:
    """Apply an explicit audit/report lane to rehydrated evidence rows."""

    if not lane:
        return results
    for result in results:
        result.benchmark_lane = lane
    return results


def _existing_jsonl_exit_code(
    results: list[DemoResult],
    *,
    require_stress_criteria: bool,
    require_lane_criteria: bool,
) -> int:
    """Return the release-gate exit code for rehydrated benchmark evidence."""

    if not results:
        return 2
    cases_passed = all(result.passed for result in results)
    audit_passed = all(item["passed"] for item in _stress_audit(results))
    lane = results[0].benchmark_lane if results else "recorded"
    lane_passed = all(item["passed"] for item in _provider_lane_audit(results, lane))
    if require_lane_criteria:
        return 0 if cases_passed and lane_passed else 1
    if require_stress_criteria:
        return 0 if cases_passed and audit_passed else 1
    return 0


def render_report_from_jsonl(
    output_jsonl: Path,
    report_path: Path,
    *,
    lane: str = "",
    require_stress_criteria: bool = False,
    require_lane_criteria: bool = False,
) -> int:
    """Render a markdown report from an existing benchmark JSONL evidence file."""
    rows = _rows_from_jsonl(output_jsonl)
    results = _apply_render_lane([_result_from_case_row(row) for row in rows], lane)
    report_path.write_text(_render_report(results, output_jsonl.resolve()), encoding="utf-8")
    return _existing_jsonl_exit_code(
        results,
        require_stress_criteria=require_stress_criteria,
        require_lane_criteria=require_lane_criteria,
    )


def render_report_from_jsonls(
    input_jsonls: list[Path],
    output_jsonl: Path,
    report_path: Path,
    *,
    lane: str = "",
    require_stress_criteria: bool = False,
    require_lane_criteria: bool = False,
) -> int:
    """Combine existing benchmark JSONL evidence files and render one report."""

    rows: list[dict[str, Any]] = []
    for path in input_jsonls:
        rows.extend(_rows_from_jsonl(path))
    results = _apply_render_lane([_result_from_case_row(row) for row in rows], lane)
    normalized_rows = [_case_row(result) for result in results]
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in normalized_rows),
        encoding="utf-8",
    )
    report_path.write_text(_render_report(results, output_jsonl.resolve()), encoding="utf-8")
    return _existing_jsonl_exit_code(
        results,
        require_stress_criteria=require_stress_criteria,
        require_lane_criteria=require_lane_criteria,
    )


def _canonical_cases_by_id() -> dict[str, DemoCase]:
    """Return benchmark cases with their full pass/fail criteria intact."""
    global _CANONICAL_CASES_BY_ID
    if _CANONICAL_CASES_BY_ID is None:
        _CANONICAL_CASES_BY_ID = {
            case.case_id: case for case in _make_cases(_canonical_benchmark_manifest())
        }
    return _CANONICAL_CASES_BY_ID


def _canonical_benchmark_manifest() -> dict[str, Any]:
    """Build a path-only manifest for report rehydration without writing fixtures."""
    root = Path("benchmark-data")
    return {
        "hdf5": {"path": str(root / "fusion_run.h5")},
        "parquet": {
            "path": str(root / "facility_measurements.parquet"),
            "dirty_path": str(root / "facility_measurements_dirty.parquet"),
        },
        "csv": {"path": str(root / "sensor_events.csv")},
        "adios": {"path": str(root / "gray scott noise 0.01 data.bp5")},
        "genomics": {
            "fasta_path": str(root / "synthetic_pathogen.fa"),
            "vcf_path": str(root / "synthetic_pathogen.vcf"),
        },
        "materials": {"cif_path": str(root / "perovskite_reference.cif")},
        "geospatial": {"geojson_path": str(root / "field_sites.geojson")},
        "imaging": {"png_path": str(root / "microscopy_cells.png")},
        "mass_spec": {"mzml_path": str(root / "proteomics_qc.mzML")},
        "lfq": {
            "lfq_path": str(root / "proteinGroups_lfq_benchmark.tsv"),
            "control_prefix": "Control",
            "treatment_prefix": "Treatment",
            "spike_terms": "SPIKEUP,SPIKEUPB",
            "expected_spike_log2fc": 2.0,
        },
        "hpc": {
            "baseline_path": str(root / "baseline_darshan.txt"),
            "candidate_path": str(root / "candidate_darshan.txt"),
        },
        "format_bridge": {
            "source_path": str(root / "format_bridge_source.h5"),
            "output_path": str(root / "format_bridge_converted.parquet"),
        },
        "terrain": {
            "dem_path": str(root / "terrain_dem.csv"),
            "pointcloud_path": str(root / "terrain_points.csv"),
            "gridded_path": str(root / "terrain_points_gridded.csv"),
        },
    }


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
    lfq = manifest["lfq"]["lfq_path"]
    lfq_control = manifest["lfq"]["control_prefix"]
    lfq_treatment = manifest["lfq"]["treatment_prefix"]
    lfq_spikes = manifest["lfq"]["spike_terms"]
    lfq_expected_fc = manifest["lfq"]["expected_spike_log2fc"]
    hpc_baseline = manifest["hpc"]["baseline_path"]
    hpc_candidate = manifest["hpc"]["candidate_path"]
    format_source = manifest["format_bridge"]["source_path"]
    format_output = manifest["format_bridge"]["output_path"]
    terrain_dem = manifest["terrain"]["dem_path"]
    terrain_pointcloud = manifest["terrain"]["pointcloud_path"]
    terrain_gridded = manifest["terrain"]["gridded_path"]
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
            expected_tool_prefixes=("hdf5_", "adios_", "parquet_", "csv_"),
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
            expected_tool_prefixes=("hdf5_", "adios_", "parquet_", "csv_"),
            min_children=3,
            timeout_s=720.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("no-guard", "planner", "nanoagents", "tier-3", "multi-file"),
            semantic_proofs=("no_shortcuts", "root_delegation", "nested_tier3"),
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
            expected_agent="data",
            expected_tool_prefixes=("ndp_",),
            expected_handoff_agents=("ndp_catalog",),
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
            expected_agent=("data", "analysis", "visualization"),
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_tools=("sac_fetch_earthscope_waveform",),
            expected_tool_prefix_groups=(("ndp_", "sac_"), ("ndp_",)),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_handoff_agent_groups=(),
            expected_terms=("SAC", ".png"),
            expected_term_groups=(),
            min_artifacts=1,
            min_expert_depth=3,
            min_branch_count=2,
            timeout_s=900.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            semantic_proofs=(
                "no_shortcuts",
                "root_delegation",
                "nested_tier3",
                "sync_parent_return",
                "failure_recovery",
            ),
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
            expected_terms=("feature", "geometry", "bounds", "property"),
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
            expected_terms=("spectra", "ms1", "ms2", "tic"),
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
            case_id="marketplace_genomics_reference_review",
            title="Marketplace genomics FASTA reference review",
            category="marketplace-genomics",
            session_group="marketplace_genomics_reference",
            agent_blueprint_id="genomics-review",
            expected_agent="main",
            expected_tools=("genomics_inspect_fasta",),
            expected_handoff_agents=("reference",),
            expected_terms=("chrA", "plasmidB"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "genomics", "fasta", "agent-blueprint"),
            prompt=(
                f"Review this reference FASTA for collaborator handoff: {fasta}. "
                "Summarize contigs, composition evidence, and what should be verified "
                "before variant interpretation."
            ),
            expected=(
                "CLIO runs the genomics-review marketplace Agent Blueprint in this "
                "session, routes through the root expert, and uses the reference "
                "expert's FASTA tool."
            ),
            why=(
                "Proves a domain agent installed from the marketplace can be activated "
                "per session and execute its own hierarchy plus expert/tool surface."
            ),
        ),
        DemoCase(
            case_id="marketplace_genomics_variant_review",
            title="Marketplace genomics VCF variant review",
            category="marketplace-genomics",
            session_group="marketplace_genomics_variants",
            agent_blueprint_id="genomics-review",
            expected_agent="main",
            expected_tools=("genomics_summarize_vcf",),
            expected_handoff_agents=("variants",),
            expected_terms=("frameshift", "stop_gained"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "genomics", "vcf", "agent-blueprint"),
            prompt=(
                f"Review this VCF for collaborator handoff: {vcf}. Summarize variant "
                "types, likely effects, and what should be verified before analysis."
            ),
            expected=(
                "CLIO runs the genomics-review marketplace Agent Blueprint in this "
                "session, routes through the root expert, and uses the variants "
                "expert's VCF tool."
            ),
            why=(
                "Exercises a second expert in the same marketplace agent, proving the "
                "active blueprint changes the available hierarchy and expert surface."
            ),
        ),
        DemoCase(
            case_id="marketplace_genomics_cohort_qc",
            title="Marketplace genomics cohort QC",
            category="marketplace-genomics",
            session_group="marketplace_genomics_cohort",
            agent_blueprint_id="genomics-review",
            expected_agent="main",
            expected_tools=("genomics_vcf_cohort_qc",),
            expected_handoff_agents=(
                "cohort_qc",
                "per_sample_metrics",
                "cohort_outliers",
                "manifest_reconciliation",
            ),
            expected_terms=("sample_A", "call rate", "heterozygosity", "manifest"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "genomics", "cohort-qc", "agent-blueprint"),
            prompt=(
                f"Review this VCF cohort for downstream QC readiness: {vcf}. "
                "Check per-sample call rate, missingness, heterozygosity, and "
                "whether any samples should be dropped before analysis."
            ),
            expected=(
                "CLIO runs the genomics-review marketplace Agent Blueprint through "
                "its cohort_qc expert, executes the per_sample_metrics -> "
                "cohort_outliers -> manifest_reconciliation child chain, and "
                "calls genomics_vcf_cohort_qc from the metric child."
            ),
            why=(
                "Brings the first-wave cohort QC benchmark infrastructure into the "
                "marketplace runner instead of leaving it as only a tool-level proof."
            ),
        ),
        DemoCase(
            case_id="marketplace_materials_crystal_review",
            title="Marketplace materials CIF readiness review",
            category="marketplace-materials",
            session_group="marketplace_materials",
            agent_blueprint_id="materials-crystal-review",
            expected_agent="main",
            expected_tools=("materials_inspect_cif",),
            expected_handoff_agents=(
                "crystal_structure",
                "symmetry_quality",
            ),
            expected_terms=("SrTiO3", "P m -3 m"),
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "materials", "cif", "agent-blueprint"),
            prompt=(
                f"Review this CIF as a materials simulation handoff: {cif}. "
                "Summarize formula, symmetry, occupancy or atom-site quality, and "
                "whether the structure is ready to spend compute time on."
            ),
            expected=(
                "CLIO runs the materials-crystal-review marketplace Agent Blueprint "
                "through its root expert, inspects the CIF with crystal_structure, "
                "and continues through symmetry_quality before final synthesis."
            ),
            why=(
                "Proves a separate materials marketplace agent can be loaded per "
                "session and can execute a non-seismic multi-expert hierarchy."
            ),
        ),
        DemoCase(
            case_id="marketplace_geospatial_field_review",
            title="Marketplace geospatial GeoJSON review",
            category="marketplace-geospatial",
            session_group="marketplace_geospatial",
            agent_blueprint_id="geospatial-field-review",
            expected_agent="main",
            expected_tools=("geospatial_inspect_geojson",),
            expected_handoff_agents=("spatial_features",),
            expected_terms=("feature", "geometry", "bounds"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "geospatial", "geojson", "agent-blueprint"),
            prompt=(
                f"Review this GeoJSON for field-site map readiness: {geojson}. "
                "Summarize feature types, bounds, properties, and map-overlay risks."
            ),
            expected=(
                "CLIO runs the geospatial-field-review marketplace Agent Blueprint "
                "through its root expert and uses the spatial_features expert."
            ),
            why=(
                "Proves a geospatial marketplace agent can be loaded per session "
                "and can delegate through its own hierarchy."
            ),
        ),
        DemoCase(
            case_id="marketplace_proteomics_mzml_review",
            title="Marketplace proteomics mzML readiness review",
            category="marketplace-proteomics",
            session_group="marketplace_proteomics",
            agent_blueprint_id="proteomics-mzml-review",
            expected_agent="main",
            expected_tools=("mass_spec_inspect_mzml",),
            expected_handoff_agents=(
                "mass_spec",
                "spectra_quality",
            ),
            expected_terms=("spectra", "tic"),
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "proteomics", "mzml", "agent-blueprint"),
            prompt=(
                f"Review this mzML run for peptide-search handoff: {mzml}. "
                "Summarize spectra, MS-level balance, m/z coverage, TIC evidence, "
                "spectra-quality risks, and whether the run is ready for search."
            ),
            expected=(
                "CLIO runs the proteomics-mzml-review marketplace Agent Blueprint "
                "through its root expert, inspects mzML with mass_spec, continues "
                "through spectra_quality, and then synthesizes peptide-search "
                "readiness from the returned evidence."
            ),
            why=(
                "Proves a proteomics marketplace agent can be loaded per session "
                "and can execute a non-seismic multi-expert hierarchy."
            ),
        ),
        DemoCase(
            case_id="marketplace_proteomics_lfq_differential",
            title="Marketplace proteomics LFQ differential abundance",
            category="marketplace-proteomics",
            session_group="marketplace_proteomics_lfq",
            agent_blueprint_id="proteomics-mzml-review",
            expected_agent="main",
            expected_tools=("mass_spec_lfq_differential_abundance",),
            expected_handoff_agents=("lfq_differential",),
            expected_terms=("SPIKEUP", "selected", "median"),
            timeout_s=620.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "proteomics", "lfq", "agent-blueprint"),
            prompt=(
                f"Review this LFQ proteinGroups matrix for differential abundance: {lfq}. "
                f"Compare columns matching {lfq_control} against {lfq_treatment}, use "
                f"spike-in terms {lfq_spikes} with an expected log2 fold change near "
                f"{lfq_expected_fc}, and tell me which proteins look most changed."
            ),
            expected=(
                "CLIO runs the proteomics marketplace Agent Blueprint through its "
                "lfq_differential expert and calls the LFQ differential-abundance tool."
            ),
            why=(
                "Exercises the first-wave proteomics LFQ decision-subtree infrastructure "
                "rather than only mzML inspection."
            ),
        ),
        DemoCase(
            case_id="marketplace_hpc_io_regression",
            title="Marketplace HPC I/O regression",
            category="marketplace-hpc",
            session_group="marketplace_hpc_regression",
            agent_blueprint_id="hpc-io-regression",
            expected_agent="main",
            expected_tools=("hpc_compare_darshan_traces",),
            expected_handoff_agents=("trace_ingest", "regression_diff"),
            expected_terms=("write_time", "regression", "independent_writes"),
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            timeout_s=720.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "hpc", "darshan", "agent-blueprint", "tier-3"),
            prompt=(
                f"Compare these two HPC I/O traces before collaborator handoff: "
                f"baseline {hpc_baseline} and candidate {hpc_candidate}. Identify "
                "the main I/O regression, stable metrics, and likely root cause."
            ),
            expected=(
                "CLIO runs the hpc-io-regression marketplace Agent Blueprint, parses "
                "both traces through its ingest path, compares them, and synthesizes "
                "root-cause evidence."
            ),
            why=(
                "Adds the first-wave HPC I/O regression case to the marketplace lane, "
                "including paired inputs and tier-3 ingest workers."
            ),
        ),
        DemoCase(
            case_id="marketplace_format_bridge_integrity",
            title="Marketplace scientific format bridge integrity",
            category="marketplace-format",
            session_group="marketplace_format_bridge",
            agent_blueprint_id="format-bridge",
            expected_agent="main",
            expected_tools=("format_convert_hdf5_to_parquet",),
            expected_handoff_agents=("source_inspect", "conversion_policy", "integrity"),
            expected_terms=("safe_float", "skipped", "checksum"),
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            timeout_s=720.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "format-bridge", "hdf5", "parquet", "agent-blueprint"),
            prompt=(
                f"Convert this scientific HDF5 table to Parquet with integrity evidence: "
                f"{format_source}. Write the output to {format_output}. Preserve row counts, "
                "flag unsafe or lossy dtype decisions, and tell me whether the conversion is "
                "safe for downstream visualization."
            ),
            expected=(
                "CLIO runs the format-bridge marketplace Agent Blueprint through inspect, "
                "conversion policy, and integrity experts, using the HDF5-to-Parquet tool."
            ),
            why=(
                "Adds the first-wave scientific format bridge case to benchmark evidence "
                "instead of relying only on unit tests for conversion policy."
            ),
        ),
        DemoCase(
            case_id="marketplace_terrain_pointcloud_suitability",
            title="Marketplace terrain point-cloud suitability",
            category="marketplace-terrain",
            session_group="marketplace_terrain",
            agent_blueprint_id="terrain-suitability",
            expected_agent="main",
            expected_tools=("terrain_pointcloud_read", "terrain_dem_terrain"),
            expected_handoff_agents=("terrain_derivation", "gridding", "suitability"),
            expected_terms=("slope", "suitable", "grid"),
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            timeout_s=720.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=("marketplace", "terrain", "point-cloud", "agent-blueprint", "tier-3"),
            prompt=(
                f"Evaluate these terrain points for site suitability: {terrain_pointcloud}. "
                f"Grid them to {terrain_gridded}, derive terrain, and identify cells with "
                "elevation between 100 and 104 meters and slope below 60 degrees. "
                f"Use the ready DEM {terrain_dem} only as a comparison if needed."
            ),
            expected=(
                "CLIO runs the terrain-suitability marketplace Agent Blueprint, routes raw "
                "point-cloud input through gridding, then applies DEM terrain suitability."
            ),
            why=(
                "Adds the first-wave terrain/lidar conditional-branch case to the runner, "
                "including the point-cloud-to-DEM path the benchmark was designed to stress."
            ),
        ),
        DemoCase(
            case_id="marketplace_seismic_waveform_review",
            title="Marketplace seismic waveform recovery review",
            category="marketplace-seismic",
            session_group="marketplace_seismic",
            agent_blueprint_id="seismic-waveform-review",
            expected_agent=("data", "analysis", "visualization", "main"),
            expected_tool_prefixes=("ndp_", "sac_"),
            expected_tools=("sac_fetch_earthscope_waveform",),
            expected_handoff_agents=("ndp_catalog", "sac_format"),
            expected_terms=("SAC", ".png"),
            min_artifacts=1,
            min_expert_depth=_MARKETPLACE_COMPLEX_MIN_EXPERT_DEPTH,
            min_branch_count=_MARKETPLACE_COMPLEX_MIN_BRANCH_COUNT,
            semantic_proofs=(
                "marketplace_pack",
                "root_delegation",
                "nested_tier3",
                "sync_parent_return",
                "failure_recovery",
            ),
            timeout_s=900.0,
            forbidden_route_sources=_REAL_ORCHESTRATOR_FORBIDDEN_SOURCES,
            complexity_tags=(
                "marketplace",
                "seismic",
                "ndp",
                "sac",
                "agent-blueprint",
                "artifact",
            ),
            prompt=(
                "Using the active seismic waveform review agent, find bounded seismic "
                "waveform evidence through NDP discovery, recover with an alternate SAC "
                "source if NDP staging is blocked, inspect the waveform, compute trace "
                "statistics, and produce a PNG plot artifact without using stale local files."
            ),
            expected=(
                "CLIO runs the seismic-waveform-review marketplace Agent Blueprint, "
                "surfaces NDP staging blockers, recovers with an observed EarthScope SAC "
                "path, and creates a verified PNG artifact."
            ),
            why=(
                "Proves the marketplace can carry the strongest hierarchical workflow, "
                "not just single-expert file inspection packages."
            ),
        ),
        DemoCase(
            case_id="marketplace_mcp_calculator_scope",
            title="Marketplace MCP descriptor scope",
            category="marketplace-mcp",
            session_group="marketplace_mcp_calculator",
            agent_blueprint_id="mcp-calculator-smoke",
            expected_agent="main",
            expected_terms=("calculator_add", "disabled", "trust"),
            semantic_proofs=("command_mcp_skill_scope",),
            complexity_tags=(
                "marketplace",
                "mcp",
                "capability-scope",
                "agent-blueprint",
            ),
            prompt=(
                "Using the active calculator MCP smoke agent, verify whether the packaged "
                "calculator capability is immediately usable. Explain the descriptor state, "
                "what tool it would expose, and what trust or enablement step is required "
                "before the tool can be called."
            ),
            expected=(
                "CLIO runs the mcp-calculator-smoke Agent Blueprint and surfaces that the "
                "pack-local calculator MCP descriptor is packaged but disabled until explicit "
                "trust/enablement, with calculator_add named as the scoped tool."
            ),
            why=(
                "Covers the semantic-regression proof for command/MCP/skill capability scoping "
                "using marketplace blueprint metadata instead of a hardcoded tool path."
            ),
        ),
        DemoCase(
            case_id="marketplace_mcp_calculator_enabled_call",
            title="Marketplace MCP enabled tool call",
            category="marketplace-mcp",
            session_group="marketplace_mcp_calculator_enabled",
            agent_blueprint_id="mcp-calculator-smoke",
            expected_terms=("calculator_add", "enabled", "7"),
            semantic_proofs=("command_mcp_skill_scope", "enabled_mcp_execution"),
            mcp_enable_descriptor_id="calculator",
            mcp_call_tool="calculator_add",
            mcp_call_args={"a": 2, "b": 5},
            expected_actions=("agent_blueprint_mcp_enable", "mcp_tool_call"),
            skip_model_turn=True,
            complexity_tags=(
                "marketplace",
                "mcp",
                "enabled-execution",
                "agent-blueprint",
            ),
            prompt=(
                "Using the active calculator MCP smoke agent, report whether the packaged "
                "calculator tool is now enabled and what result was observed for adding "
                "2 and 5."
            ),
            expected=(
                "CLIO explicitly trusts and enables the pack-local calculator MCP descriptor, "
                "probes calculator_add as a ready tool, calls it through the MCP server endpoint, "
                "and records a successful result before the model-facing turn."
            ),
            why=(
                "Proves the marketplace pack can provide a self-contained external MCP tool "
                "surface that CLIO can launch and call, not just advertise as metadata."
            ),
        ),
        DemoCase(
            case_id="marketplace_packaged_hook_blocked_turn",
            title="Marketplace packaged hook blocked turn",
            category="marketplace-hooks",
            session_group="marketplace_hook_smoke",
            agent_blueprint_id="hook-smoke",
            expected_terms=("pre_message", "blocked", "agent_blueprint"),
            semantic_proofs=("packaged_hook_invocation",),
            hook_enable_id="pre_message",
            hook_probe_text="CLIO_HOOK_SMOKE_BLOCK prove packaged hook invocation",
            expected_actions=("agent_blueprint_hook_enable", "packaged_hook_probe"),
            skip_model_turn=True,
            complexity_tags=(
                "marketplace",
                "hooks",
                "explicit-trust",
                "semantic-trace",
                "agent-blueprint",
            ),
            prompt=(
                "Using the active hook smoke agent, verify that the packaged pre_message "
                "hook is disabled by default, explicitly enabled with trust, and invoked "
                "with packaged provenance during a blocked benchmark probe."
            ),
            expected=(
                "CLIO exposes the hook-smoke packaged hook descriptor, explicitly trusts "
                "and enables pre_message, then records hook.pre_message.blocked semantic "
                "events whose handler provenance points back to the Agent Blueprint hook file."
            ),
            why=(
                "Closes the marketplace packaged-hook evidence gap without relying on final "
                "answer wording or a provider call."
            ),
        ),
        DemoCase(
            case_id="workspace_memory_scope_policy",
            title="Workspace memory scope policy",
            category="memory-scope",
            session_group="workspace_memory_scope",
            expected_terms=(
                "allow_same_workspace_user_intent",
                "deny_cross_session_requires_intent",
                "deny_other_workspace",
            ),
            semantic_proofs=("workspace_memory_scope",),
            expected_actions=("workspace_memory_scope_probe",),
            memory_scope_probe=True,
            skip_model_turn=True,
            complexity_tags=(
                "memory",
                "workspace-scope",
                "policy",
                "semantic-regression",
            ),
            prompt=(
                "Using the current workspace, prove that cross-session memory search "
                "requires explicit user intent, same-workspace memory can be read with "
                "that intent, and another workspace's session memory does not leak."
            ),
            expected=(
                "CLIO records denied cross-session memory search without intent, allows "
                "same-workspace memory search with user intent and provenance, and denies "
                "a read of another workspace's session summary."
            ),
            why=(
                "Turns workspace memory compartmentalization from API-only tests into "
                "committed benchmark evidence for the 1.0 semantic regression lane."
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
            semantic_proofs=("workspace_memory_scope",),
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
    blueprint = case.agent_blueprint_id or "builtin"
    return f"{case.session_group}:{case.routing_mode}:{blueprint}"


def _ensure_benchmark_workspace(http: httpx.Client, root_path: Path) -> str:
    """Create or reuse a workspace rooted at the benchmark checkout."""

    root = str(root_path.expanduser().resolve())
    response = http.get("/v1/workspaces")
    response.raise_for_status()
    for row in response.json().get("workspaces", []):
        if str(row.get("root_path") or "") == root:
            return str(row.get("id") or "")

    created = http.post(
        "/v1/workspaces",
        json={
            "name": "CLIO marketplace benchmark",
            "root_path": root,
            "storage_root": str(Path(root) / ".clio"),
        },
    )
    created.raise_for_status()
    return str(created.json().get("id") or "")


def _install_marketplace_blueprints(http: httpx.Client, source: str, *, workspace_id: str) -> None:
    """Install all Agent Blueprints from a marketplace source into workspace scope."""
    if not source:
        raise ValueError(
            "marketplace benchmark cases require --marketplace-source or CLIO_MARKETPLACE_SOURCE"
        )
    response = http.post(
        "/v1/agent-blueprints/install",
        json={"source": source, "scope": "workspace", "workspace_id": workspace_id},
        timeout=180.0,
    )
    response.raise_for_status()


def _create_sessions(
    http: httpx.Client,
    cases: list[DemoCase],
    *,
    workspace_id: str = "",
) -> dict[str, str]:
    session_ids: dict[str, str] = {}
    for key in dict.fromkeys(_session_key(case) for case in cases):
        group, routing_mode, blueprint_id = key.rsplit(":", 2)
        payload = {"title": f"demo {group}"}
        if workspace_id:
            payload["workspace_id"] = workspace_id
        if routing_mode != "auto":
            payload["routing_mode"] = routing_mode
        response = http.post("/v1/sessions", json=payload)
        response.raise_for_status()
        session_id = response.json()["id"]
        if blueprint_id != "builtin":
            blueprint_response = http.post(
                f"/v1/sessions/{session_id}/agent-blueprint",
                json={"blueprint_id": blueprint_id},
            )
            blueprint_response.raise_for_status()
        session_ids[key] = session_id
    return session_ids


def _session_agent_blueprint(http: httpx.Client, session_id: str) -> dict[str, Any]:
    response = http.get(f"/v1/sessions/{session_id}/agent-blueprint")
    if response.status_code >= 400:
        return {"error": response.text}
    body = response.json()
    return body if isinstance(body, dict) else {}


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
    "marketplace_agents": (
        "marketplace_genomics_reference_review",
        "marketplace_genomics_variant_review",
        "marketplace_genomics_cohort_qc",
        "marketplace_materials_crystal_review",
        "marketplace_geospatial_field_review",
        "marketplace_proteomics_mzml_review",
        "marketplace_proteomics_lfq_differential",
        "marketplace_hpc_io_regression",
        "marketplace_format_bridge_integrity",
        "marketplace_terrain_pointcloud_suitability",
        "marketplace_seismic_waveform_review",
    ),
    "semantic_regression": (
        "reasoning_cross_file_triage_nanoagents",
        "ndp_seismic_waveform_to_plot",
        "marketplace_seismic_waveform_review",
        "marketplace_mcp_calculator_scope",
        "marketplace_mcp_calculator_enabled_call",
        "marketplace_packaged_hook_blocked_turn",
        "workspace_memory_scope_policy",
        "provider_swap_memory_followup",
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
    if lane == "marketplace_agents":
        return "CLIO Marketplace Agent Benchmark Report"
    if lane == "semantic_regression":
        return "CLIO 1.0 Semantic Regression Benchmark Report"
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
    marketplace_source: str = "",
    watch_events: bool = False,
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
        workspace_id = ""
        if any(case.agent_blueprint_id or case.memory_scope_probe for case in cases):
            workspace_id = _ensure_benchmark_workspace(http, Path.cwd())
        if any(case.agent_blueprint_id for case in cases):
            _install_marketplace_blueprints(
                http,
                marketplace_source,
                workspace_id=workspace_id,
            )
        session_ids = _create_sessions(http, cases, workspace_id=workspace_id)
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
                if case.mcp_enable_descriptor_id:
                    action = _enable_blueprint_mcp_for_case(
                        http,
                        case,
                        workspace_id=workspace_id,
                        timeout_s=case.timeout_s,
                    )
                    actions.append(action)
                    print(
                        f"  MCP enable {'ok' if action.get('ok') else 'failed'} "
                        f"descriptor={case.mcp_enable_descriptor_id} "
                        f"status={action.get('status') or '-'} "
                        f"elapsed={action.get('elapsed_s')}s",
                        flush=True,
                    )
                    if case.mcp_call_tool:
                        call_action = _call_enabled_mcp_for_case(
                            http,
                            case,
                            action,
                            timeout_s=case.timeout_s,
                        )
                        actions.append(call_action)
                        print(
                            f"  MCP call {'ok' if call_action.get('ok') else 'failed'} "
                            f"tool={case.mcp_call_tool} "
                            f"elapsed={call_action.get('elapsed_s')}s",
                            flush=True,
                        )
                if case.hook_enable_id:
                    action = _enable_blueprint_hook_for_case(
                        http,
                        case,
                        workspace_id=workspace_id,
                        timeout_s=case.timeout_s,
                    )
                    actions.append(action)
                    print(
                        f"  hook enable {'ok' if action.get('ok') else 'failed'} "
                        f"hook={case.hook_enable_id} "
                        f"status={action.get('status') or '-'} "
                        f"elapsed={action.get('elapsed_s')}s",
                        flush=True,
                    )
                    if case.hook_probe_text:
                        probe_action = _probe_packaged_hook_for_case(
                            http,
                            case,
                            session_id=session_id,
                            timeout_s=case.timeout_s,
                        )
                        actions.append(probe_action)
                        print(
                            f"  hook probe {'ok' if probe_action.get('ok') else 'failed'} "
                            f"hook={case.hook_enable_id} "
                            f"elapsed={probe_action.get('elapsed_s')}s",
                            flush=True,
                        )
                if case.memory_scope_probe:
                    action = _probe_workspace_memory_scope_for_case(
                        http,
                        case,
                        session_id=session_id,
                        workspace_id=workspace_id,
                        timeout_s=case.timeout_s,
                    )
                    actions.append(action)
                    print(
                        f"  memory scope {'ok' if action.get('ok') else 'failed'} "
                        f"checks={len(action.get('checks') or [])} "
                        f"elapsed={action.get('elapsed_s')}s",
                        flush=True,
                    )
                provider = _provider(http)
                started = time.monotonic()
                if case.skip_model_turn:
                    message = _action_only_message(case, actions)
                else:
                    with _LiveEventWatch(base_url, session_id, enabled=watch_events):
                        message = _post_turn(
                            http,
                            session_id,
                            case.prompt,
                            timeout_s=case.timeout_s,
                            cancel_after_s=case.cancel_after_s,
                            agent_id=_turn_agent_id_for_lane(case, lane),
                        )
                elapsed_s = time.monotonic() - started
                semantic_events = (
                    [
                        dict(event)
                        for action in actions
                        for event in (action.get("semantic_events") or [])
                        if isinstance(event, Mapping)
                    ]
                    if case.skip_model_turn
                    else _semantic_events_for_completed_message(
                        http,
                        session_id,
                        str(message.get("id") or ""),
                    )
                )
                after_children = _children(http, session_id)
                new_children = [
                    child for child in after_children if child.get("id") not in before_children
                ]
                session_messages = _chronological_session_messages(http, session_id)
                agent_blueprint = _session_agent_blueprint(http, session_id)
                child_session_messages = {
                    str(child["id"]): _chronological_session_messages(http, str(child["id"]))
                    for child in new_children
                    if child.get("id")
                }
                result = DemoResult(
                    case=case,
                    session_id=session_id,
                    elapsed_s=elapsed_s,
                    message=message,
                    provider=provider,
                    child_sessions=new_children,
                    session_messages=session_messages,
                    child_session_messages=child_session_messages,
                    setup_messages=setup_messages,
                    actions=actions,
                    benchmark_lane=lane,
                    agent_blueprint=agent_blueprint,
                    semantic_events=semantic_events,
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


def _semantic_regression_audit(results: list[DemoResult]) -> list[dict[str, Any]]:
    """Evaluate the 1.0 semantic-regression evidence contract."""

    declared = sorted({proof for result in results for proof in result.case.semantic_proofs})
    observed_by_proof: dict[str, list[str]] = {
        proof: [] for proof in _SEMANTIC_REGRESSION_REQUIRED_PROOFS
    }
    missing_case_proofs: list[str] = []
    for result in results:
        observed = set(_case_observed_semantic_proofs(result))
        for proof in observed:
            observed_by_proof.setdefault(proof, []).append(result.case.case_id)
        for proof in result.case.semantic_proofs:
            if proof not in observed:
                missing_case_proofs.append(
                    f"{result.case.case_id}: {proof} declared but not observed"
                )

    missing_declared = sorted(set(_SEMANTIC_REGRESSION_REQUIRED_PROOFS) - set(declared))
    missing_observed = sorted(
        proof
        for proof in _SEMANTIC_REGRESSION_REQUIRED_PROOFS
        if not observed_by_proof.get(proof)
    )
    passing_results = [result for result in results if result.passed]
    missing_route_evidence = [
        result
        for result in passing_results
        if not result.route_graph["nodes"] or result.route_metrics["expert_depth"] <= 0
    ]
    missing_sync_return_evidence = [
        result for result in passing_results if _missing_sync_return_pairs(result)
    ]
    forbidden = [
        result for result in results if result.route_source in result.case.forbidden_route_sources
    ]
    return [
        {
            "criterion": "semantic-regression lane declares required proof classes",
            "observed": len(_SEMANTIC_REGRESSION_REQUIRED_PROOFS) - len(missing_declared),
            "required": len(_SEMANTIC_REGRESSION_REQUIRED_PROOFS),
            "passed": not missing_declared,
            "details": [
                f"{proof}: {_SEMANTIC_REGRESSION_REQUIRED_PROOFS[proof]}"
                for proof in missing_declared
            ],
        },
        {
            "criterion": "semantic-regression passing evidence covers required proof classes",
            "observed": len(_SEMANTIC_REGRESSION_REQUIRED_PROOFS) - len(missing_observed),
            "required": len(_SEMANTIC_REGRESSION_REQUIRED_PROOFS),
            "passed": not missing_observed,
            "details": [
                f"{proof}: {_SEMANTIC_REGRESSION_REQUIRED_PROOFS[proof]}"
                for proof in missing_observed
            ],
        },
        {
            "criterion": "each declared case proof is observed in session evidence",
            "observed": sum(len(proofs) for proofs in observed_by_proof.values()),
            "required": sum(len(result.case.semantic_proofs) for result in results),
            "passed": not missing_case_proofs,
            "details": missing_case_proofs,
        },
        {
            "criterion": "semantic-regression cases avoid shortcut route sources",
            "observed": len(results) - len(forbidden),
            "required": len(results),
            "passed": not forbidden,
            "details": [
                f"{result.case.case_id}: route_source={result.route_source}"
                for result in forbidden
            ],
        },
        {
            "criterion": "passing semantic-regression cases include route evidence",
            "observed": len(passing_results) - len(missing_route_evidence),
            "required": len(passing_results),
            "passed": not missing_route_evidence,
            "details": [
                f"{result.case.case_id}: route_metrics={result.route_metrics}"
                for result in missing_route_evidence
            ],
        },
        {
            "criterion": "nested semantic-regression delegations include sync return/resume",
            "observed": len(passing_results) - len(missing_sync_return_evidence),
            "required": len(passing_results),
            "passed": not missing_sync_return_evidence,
            "details": [
                f"{result.case.case_id}: missing={_missing_sync_return_pairs(result)}"
                for result in missing_sync_return_evidence
            ],
        },
        {
            "criterion": "observed semantic proof coverage by case",
            "observed": ", ".join(
                f"{proof}={observed_by_proof.get(proof) or []}"
                for proof in sorted(_SEMANTIC_REGRESSION_REQUIRED_PROOFS)
            ),
            "required": "reported",
            "passed": True,
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

    if lane == "semantic_regression":
        return _semantic_regression_audit(results)

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
        missing_sync_return_evidence = [
            result
            for result in passing_results
            if _missing_sync_return_pairs(result)
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
                "criterion": "nested expert handoffs include sync return/resume provenance",
                "observed": len(passing_results) - len(missing_sync_return_evidence),
                "required": len(passing_results),
                "passed": not missing_sync_return_evidence,
                "details": [
                    f"{result.case.case_id}: missing={_missing_sync_return_pairs(result)}"
                    for result in missing_sync_return_evidence
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

    if lane == "marketplace_agents":
        active = {
            result.active_agent_blueprint_id
            for result in results
            if result.active_agent_blueprint_id
        }
        missing_blueprint = [
            result.case.case_id
            for result in results
            if result.case.agent_blueprint_id
            and result.active_agent_blueprint_id != result.case.agent_blueprint_id
        ]
        tool_backed = [result for result in results if result.tool_names]
        missing_root_orchestration = [
            result
            for result in results
            if result.case.agent_blueprint_id
            and result.case.expected_handoff_agents
            and (
                result.case.expected_handoff_agents[0] not in result.handoff_agent_ids
                or _missing_sync_return_pairs(result)
            )
        ]
        complex_hierarchy = [
            result for result in results if _meets_complex_hierarchy_threshold(result)
        ]
        shallow_hierarchy = [
            result
            for result in results
            if result.case.agent_blueprint_id
            and result.passed
            and not _meets_complex_hierarchy_threshold(result)
        ]
        return [
            {
                "criterion": "all marketplace cases prove the requested active Agent Blueprint",
                "observed": len(results) - len(missing_blueprint),
                "required": len(results),
                "passed": not missing_blueprint,
                "details": missing_blueprint,
            },
            {
                "criterion": "at least five distinct marketplace Agent Blueprints",
                "observed": len(active),
                "required": 5,
                "passed": len(active) >= 5,
                "details": sorted(active),
            },
            {
                "criterion": "all marketplace cases call at least one blueprint expert tool",
                "observed": len(tool_backed),
                "required": len(results),
                "passed": len(tool_backed) == len(results),
                "details": [
                    f"{result.case.case_id}: tools={result.tool_names}"
                    for result in results
                    if not result.tool_names
                ],
            },
            {
                "criterion": "marketplace hierarchy cases prove root sync delegation",
                "observed": len(results) - len(missing_root_orchestration),
                "required": len(results),
                "passed": not missing_root_orchestration,
                "details": [
                    (
                        f"{result.case.case_id}: selected={result.selected_agent or '-'} "
                        f"handoffs={result.handoff_agent_ids} "
                        f"missing_returns={_missing_sync_return_pairs(result)}"
                    )
                    for result in missing_root_orchestration
                ],
            },
            {
                "criterion": "at least three marketplace cases prove complex hierarchy depth",
                "observed": len(complex_hierarchy),
                "required": _MARKETPLACE_COMPLEX_REQUIRED_CASES,
                "passed": len(complex_hierarchy) >= _MARKETPLACE_COMPLEX_REQUIRED_CASES,
                "details": [
                    (
                        f"{result.case.case_id}: depth={result.route_metrics['expert_depth']} "
                        f"branches={result.route_metrics['branch_count']} "
                        f"sync_handoffs={result.route_metrics['sync_handoff_count']}"
                    )
                    for result in complex_hierarchy
                ],
            },
            {
                "criterion": "marketplace shallow cases are reported as smoke coverage",
                "observed": len(shallow_hierarchy),
                "required": "reported",
                "passed": True,
                "details": [
                    (
                        f"{result.case.case_id}: depth={result.route_metrics['expert_depth']} "
                        f"branches={result.route_metrics['branch_count']} "
                        "counts as load/tool smoke, not complex hierarchy proof"
                    )
                    for result in shallow_hierarchy
                ],
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
    session_logs = [result for result in results if result.session_messages]
    child_session_logs = sum(len(result.child_session_messages) for result in results)
    semantic_traced = [
        result for result in results if result.semantic_trace_summary["event_count"] > 0
    ]
    semantic_event_count = sum(
        int(result.semantic_trace_summary["event_count"]) for result in results
    )
    semantic_live_count = sum(
        int(result.semantic_trace_summary["live_event_count"]) for result in results
    )
    semantic_event_types = sorted(
        {
            event_type
            for result in results
            for event_type in result.semantic_trace_summary["unique_event_types"]
        }
    )
    declared_semantic_proofs = sorted(
        {proof for result in results for proof in result.case.semantic_proofs}
    )
    observed_semantic_proofs = sorted(
        {proof for result in results for proof in _case_observed_semantic_proofs(result)}
    )
    lines = [
        f"# {_lane_title(lane)}",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Evidence JSONL: `{output_jsonl}`",
        f"Benchmark lane: `{lane}`",
        "",
        (
            "This is a CLIO session-evidence audit. It is produced from real "
            "session JSONL rows. Review the embedded `session_log` root and child "
            "messages for prompt, route, tool, artifact, error, recovery, and "
            "final-answer evidence. Pytest coverage only guards the harness and "
            "tools; it is not the benchmark result."
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
            f"- Root session logs captured: {len(session_logs)}/{len(results)}",
            f"- Child session logs captured: {child_session_logs}",
            (
                "- Semantic trace events captured: "
                f"{semantic_event_count} events across {len(semantic_traced)}/{len(results)} cases "
                f"({semantic_live_count} live-observed)"
            ),
            (
                "- Semantic event types: "
                f"{', '.join(semantic_event_types) if semantic_event_types else 'none'}"
            ),
            (
                "- Declared semantic proofs: "
                f"{', '.join(declared_semantic_proofs) if declared_semantic_proofs else 'none'}"
            ),
            (
                "- Observed semantic proofs: "
                f"{', '.join(observed_semantic_proofs) if observed_semantic_proofs else 'none'}"
            ),
        ]
    )
    active_blueprints = sorted(
        {
            result.active_agent_blueprint_id
            for result in results
            if result.active_agent_blueprint_id
        }
    )
    if active_blueprints:
        lines.append(f"- Active Agent Blueprints: {', '.join(active_blueprints)}")
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
    if declared_semantic_proofs:
        lines.extend(
            [
                "",
                "## Semantic Proof Declarations",
                "",
                "| Case | Declared | Observed |",
                "| --- | --- | --- |",
            ]
        )
        for result in results:
            lines.append(
                "| "
                + " | ".join(
                    [
                        result.case.case_id,
                        ", ".join(result.case.semantic_proofs) or "-",
                        ", ".join(_case_observed_semantic_proofs(result)) or "-",
                    ]
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## All Cases",
            "",
            "| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
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
                    result.active_agent_blueprint_id or "-",
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
                f"Active Agent Blueprint: `{result.active_agent_blueprint_id or '-'}`",
                f"Provider/model: {_provider_model_summary(result.provider)}",
                f"Provider settings: {_provider_settings_summary(result.provider)}",
                f"Route graph: {_route_graph_summary(result)}",
                (
                    "Route metrics: "
                    f"depth={result.route_metrics['expert_depth']}, "
                    f"branches={result.route_metrics['branch_count']}, "
                    f"sync_handoffs={result.route_metrics.get('sync_handoff_count', 0)}, "
                    f"child_sessions={result.route_metrics.get('child_session_branch_count', 0)}, "
                    f"tools={result.route_metrics['tool_call_count']}"
                ),
                (
                    "Semantic trace: "
                    f"{result.semantic_trace_summary['event_count']} events, "
                    f"{result.semantic_trace_summary['live_event_count']} live, "
                    f"types={', '.join(result.semantic_trace_summary['unique_event_types']) or 'none'}"
                ),
                f"Expert handoffs: {handoff_text}",
                f"Tools: {tool_text}",
                f"Data/input files: {', '.join(result.data_files) or 'none'}",
                f"Setup turns: {len(result.setup_messages)}",
                f"Root session messages: {len(result.session_messages)}",
                f"Child session logs: {len(result.child_session_messages)}",
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
                result.visible_text[:900].strip() or "<no assistant text>",
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
            "- Planner-selected tool actions used to make benchmark evidence look flat; reports now preserve parent-owned sync delegation returns such as `data -> ndp_catalog -> data` and audit missing parent-resume evidence.",
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
    """Return a compact route graph from parent-owned handoffs and children."""
    graph = result.route_graph
    rendered_paths: list[str] = []

    def append_path(path: str) -> None:
        if path and path not in rendered_paths:
            rendered_paths.append(path)

    for edge in graph.get("edges", []):
        if edge.get("kind") != "route":
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source and target:
            append_path(f"{source} -> {target}")

    sync_pairs = set(_sync_delegation_pairs(result))
    for parent_id, child_id in sync_pairs:
        append_path(f"{parent_id} -> {child_id} -> {parent_id}")

    for edge in graph.get("edges", []):
        if edge.get("kind") != "handoff":
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source and target and (source, target) not in sync_pairs:
            append_path(f"{source} -> {target}")

    if not rendered_paths and result.selected_agent:
        rendered_paths.append(result.selected_agent)
    child_names = [
        str(child.get("title") or child.get("name") or child.get("id") or "")
        for child in result.child_sessions
    ]
    child_names = [name for name in child_names if name]
    graph_text = "; ".join(rendered_paths) if rendered_paths else "-"
    if child_names:
        graph_text += " -> [" + ", ".join(child_names) + "]"
    return graph_text


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
        default=Path("/tmp/clio-benchmark-data"),
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
        action="append",
        default=None,
        help=(
            "Render a report from one or more existing benchmark JSONL files without "
            "running cases. May be supplied multiple times; combined evidence is "
            "written to --output-jsonl."
        ),
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
    parser.add_argument(
        "--marketplace-source",
        default=os.environ.get("CLIO_MARKETPLACE_SOURCE", ""),
        help="Path or git URL for marketplace Agent Blueprints used by marketplace lanes.",
    )
    parser.add_argument(
        "--watch-events",
        action="store_true",
        help=(
            "Print a compact live SSE trace while each benchmark turn is running. "
            "Intended for manual/demo-readiness operation, not CI."
        ),
    )
    args = parser.parse_args()
    if args.render_existing_jsonl is not None:
        existing = [path.resolve() for path in args.render_existing_jsonl]
        render_lane = (
            args.lane if args.require_lane_criteria or args.lane != "real_orchestrator" else ""
        )
        if len(existing) == 1:
            code = render_report_from_jsonl(
                existing[0],
                args.report.resolve(),
                lane=render_lane,
                require_stress_criteria=args.require_stress_criteria,
                require_lane_criteria=args.require_lane_criteria,
            )
        else:
            code = render_report_from_jsonls(
                existing,
                args.output_jsonl.resolve(),
                args.report.resolve(),
                lane=render_lane,
                require_stress_criteria=args.require_stress_criteria,
                require_lane_criteria=args.require_lane_criteria,
            )
        raise SystemExit(code)
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
            marketplace_source=args.marketplace_source,
            watch_events=args.watch_events,
        )
    )


if __name__ == "__main__":
    main()
