"""Core orchestration contracts for the CLIO agent harness."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from clio_agent.arc.schema import ToolCall
from clio_agent.scientific_suffixes import scientific_suffix_alternation

RouteTarget = str
RouteSource = Literal["deterministic", "dspy", "guard", "recovery"]
ExpertSource = Literal["deterministic", "dspy", "fallback"]

# Generic path-detection regex: suffixes recognized when extracting candidate
# file paths from free text. Structural grounding only (is a file referenced),
# NOT keyword->format inference — no branch depends on which alternative matched.
# Derived from the shared vocabulary (single source of truth — issue #765).
SCIENTIFIC_PATH_SUFFIX_PATTERN = scientific_suffix_alternation()

FILE_PATH_RE = re.compile(
    rf"(?P<path>(?:~|/|\.{{1,2}}/)?[^\s'\"`]+?\.{SCIENTIFIC_PATH_SUFFIX_PATTERN})",
    re.IGNORECASE,
)
QUOTED_FILE_PATH_RE = re.compile(
    rf"(?P<quote>['\"`])(?P<path>.+?\.{SCIENTIFIC_PATH_SUFFIX_PATTERN})(?P=quote)",
    re.IGNORECASE,
)
WINDOWS_FILE_PATH_RE = re.compile(
    rf"(?<![A-Za-z])(?P<path>[A-Za-z]:[^\r\n'\"`]*?\.{SCIENTIFIC_PATH_SUFFIX_PATTERN})",
    re.IGNORECASE,
)
ROOTED_FILE_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])(?P<path>(?:~|/|\.{{1,2}}/)[^\r\n'\"`]*?\.{SCIENTIFIC_PATH_SUFFIX_PATTERN})",
    re.IGNORECASE,
)

SPECIAL_ROUTE_TARGETS: tuple[RouteTarget, ...] = (
    "chat",
    "none",
)


@dataclass(frozen=True)
class RouteDecision:
    """Validated routing decision for one user request."""

    target: RouteTarget
    source: RouteSource
    reason: str
    confidence: float
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_dspy(
        cls,
        raw_target: Any,
        *,
        available_targets: Sequence[str] | None = None,
    ) -> "RouteDecision":
        """Normalize and validate a DSPy-selected route target.

        Agent route targets are runtime registry entries, not a closed enum.
        Callers with a live registry should pass ``available_targets`` so newly
        installed experts or skills validate without code changes.
        """
        target = str(raw_target or "").strip().lower()
        valid_targets = {
            str(item).strip().lower()
            for item in (available_targets or SPECIAL_ROUTE_TARGETS)
            if str(item).strip()
        }
        if target in valid_targets:
            return cls(
                target=target,
                source="dspy",
                reason="DSPy planner selected a valid CLIO route.",
                confidence=0.7,
            )
        raise ValueError(
            f"Planner produced invalid route target {target!r}. "
            f"Expected one of: {', '.join(sorted(valid_targets))}."
        )


@dataclass
class ToolObservation:
    """A concrete tool call observed during one CLIO run."""

    tool: str
    params: dict[str, Any]
    result: Any
    duration_ms: float
    ok: bool

    def to_arc_tool_call(self) -> ToolCall:
        """Convert to the ARC invocation schema."""
        return ToolCall(
            tool=self.tool,
            params=self.params,
            result=compact_tool_result(self.result, tool=self.tool, ok=self.ok),
            duration_ms=self.duration_ms,
            cached=False,
        )


@dataclass(frozen=True)
class ExpertHandoff:
    """A concrete expert or child-expert invocation observed during one CLIO run."""

    agent_id: str
    parent_id: str | None
    dispatch_target: str
    stage: str
    status: Literal["success", "failure"]
    input_summary: str
    output_summary: str = ""
    duration_ms: float = 0.0
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe benchmark/API representation."""
        row: dict[str, Any] = {
            "agent_id": self.agent_id,
            "dispatch_target": self.dispatch_target,
            "stage": self.stage,
            "status": self.status,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "duration_ms": self.duration_ms,
            "metadata": _json_safe(self.metadata),
        }
        if self.parent_id:
            row["parent_id"] = self.parent_id
        if self.error:
            row["error"] = self.error
        return row


@dataclass(frozen=True)
class ExpertRequest:
    """Typed expert input contract used by native CLIO expert modules."""

    question: str
    file_context: str = ""
    route: RouteDecision | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ExpertResult:
    """Typed expert output contract with explicit tool provenance."""

    analysis: str
    recommendations: str
    source: ExpertSource
    tools: tuple[ToolObservation, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedToolResult:
    """Validated native tool payload or a normalized contract error."""

    tool: str
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Return whether validation succeeded."""
        return self.error is None and self.data is not None


@dataclass
class RunTrace:
    """Per-request execution trace used by the harness and ARC."""

    route: RouteDecision
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    tools: list[ToolObservation] = field(default_factory=list)
    expert_handoffs: list[ExpertHandoff] = field(default_factory=list)

    def record_tool(
        self,
        *,
        tool: str,
        params: Mapping[str, Any],
        result: Any,
        duration_ms: float,
        ok: bool,
    ) -> None:
        """Append a tool observation to this run."""
        self.tools.append(
            ToolObservation(
                tool=tool,
                params=dict(params),
                result=result,
                duration_ms=duration_ms,
                ok=ok,
            )
        )

    def record_expert_handoff(
        self,
        *,
        agent_id: str,
        parent_id: str | None,
        dispatch_target: str,
        stage: str,
        status: Literal["success", "failure"],
        input_summary: str,
        output_summary: str = "",
        duration_ms: float = 0.0,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Append an expert handoff observation to this run."""
        self.expert_handoffs.append(
            ExpertHandoff(
                agent_id=agent_id,
                parent_id=parent_id,
                dispatch_target=dispatch_target,
                stage=stage,
                status=status,
                input_summary=input_summary,
                output_summary=output_summary,
                duration_ms=duration_ms,
                error=error,
                metadata=dict(metadata or {}),
            )
        )

    @property
    def duration_ms(self) -> float:
        """Elapsed wall-clock duration for this trace."""
        return (time.time() - self.started_at) * 1000


def tool_result_ok(result: Any) -> bool:
    """Return whether a tool result represents success."""
    if isinstance(result, dict) and "error" in result:
        return False
    if isinstance(result, str) and result.startswith("Error:"):
        return False
    return True


def extract_file_paths(
    question: str, file_context: str, suffixes: AbstractSet[str]
) -> list[Path]:
    """Extract file paths with one of the requested suffixes.

    Paths explicitly provided in the user question are kept even if they do not
    exist so tools can return the policy or file-read error. Paths recovered
    from ARC file context must still exist, avoiding stale profile paths.
    """
    paths: list[Path] = []
    seen: set[str] = set()

    def add_path(raw_path: str, *, include_missing: bool) -> None:
        raw_path = raw_path.rstrip(".,;:)]}")
        path = Path(raw_path).expanduser()
        if path.suffix.lower() not in suffixes:
            return
        if not path.is_absolute():
            path = path.resolve()
        if not include_missing and not path.exists():
            return
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)

    def add_matches(text: str, *, include_missing: bool) -> None:
        candidates: list[tuple[int, int, int, str]] = []
        for match in QUOTED_FILE_PATH_RE.finditer(text):
            candidates.append((match.start(), match.end(), 0, match.group("path")))
        for match in ROOTED_FILE_PATH_RE.finditer(text):
            candidates.append((match.start(), match.end(), 1, match.group("path")))
        for match in WINDOWS_FILE_PATH_RE.finditer(text):
            candidates.append((match.start(), match.end(), 2, match.group("path")))
        for match in FILE_PATH_RE.finditer(text):
            candidates.append((match.start(), match.end(), 3, match.group("path")))

        handled_spans: list[tuple[int, int]] = []
        for start, end, _priority, raw_path in sorted(candidates):
            if any(span_start <= start < span_end for span_start, span_end in handled_spans):
                continue
            handled_spans.append((start, end))
            add_path(raw_path, include_missing=include_missing)

    add_matches(question, include_missing=True)
    add_matches(file_context, include_missing=False)
    return paths


def format_bytes(size: int) -> str:
    """Format byte counts for compact terminal/API answers."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def normalize_tool_error(
    error: Any,
    *,
    tool: str | None = None,
    code: str = "tool_failed",
    next_action: str = "Check the tool arguments and gateway health, then retry.",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize all native tool failures to one ARC/API-compatible shape."""
    if isinstance(error, MappingABC) and "error" in error:
        return normalize_tool_error(
            error["error"],
            tool=tool,
            code=code,
            next_action=next_action,
            details=details,
        )

    if isinstance(error, str):
        parsed_error = _parse_json_error_text(error)
        if parsed_error is not None:
            return normalize_tool_error(
                parsed_error,
                tool=tool,
                code=code,
                next_action=next_action,
                details=details,
            )

    normalized: dict[str, Any]
    if isinstance(error, MappingABC):
        normalized = {
            "type": str(error.get("type") or "tool_error"),
            "code": str(error.get("code") or code),
            "message": str(error.get("message") or error),
            "next_action": str(error.get("next_action") or next_action),
        }
        for key in ("field", "path"):
            value = error.get(key)
            if value is not None:
                normalized[key] = str(value)
        if error.get("handled") is not None:
            normalized["handled"] = bool(error.get("handled"))
        if error.get("handled_reason") is not None:
            normalized["handled_reason"] = str(error.get("handled_reason"))
        if error.get("details") is not None:
            normalized["details"] = _json_safe(error["details"])
    else:
        normalized = {
            "type": "tool_error",
            "code": code,
            "message": str(error) if error else "Tool returned an error.",
            "next_action": next_action,
        }

    if tool:
        normalized["tool"] = tool
    if details:
        existing = normalized.get("details")
        if isinstance(existing, dict):
            existing.update(_json_safe(details))
        else:
            normalized["details"] = _json_safe(details)
    return normalized


def normalize_tool_result(result: Any, *, tool: str | None = None) -> Any:
    """Return a tool result with normalized error payloads."""
    if isinstance(result, MappingABC) and "error" in result:
        normalized = dict(result)
        normalized["error"] = normalize_tool_error(result["error"], tool=tool)
        return normalized
    if isinstance(result, str) and result.startswith("Error:"):
        return {
            "error": normalize_tool_error(
                result.removeprefix("Error:").strip(),
                tool=tool,
            )
        }
    return result


def validate_tool_result(
    tool: str,
    result: Any,
    required_fields: Mapping[str, type | tuple[type, ...]],
) -> ValidatedToolResult:
    """Validate a native tool result mapping before answer construction."""
    normalized = normalize_tool_result(result, tool=tool)
    if isinstance(normalized, MappingABC) and "error" in normalized:
        return ValidatedToolResult(tool=tool, error=normalized["error"])
    if not isinstance(normalized, dict):
        return _validation_failure(
            tool,
            "invalid_result_type",
            f"{tool} returned {type(normalized).__name__}, expected object.",
            {"received_type": type(normalized).__name__},
        )

    field_error = _validate_fields(tool, normalized, required_fields)
    if field_error:
        return ValidatedToolResult(tool=tool, error=field_error)
    return ValidatedToolResult(tool=tool, data=normalized)


def validate_tool_items(
    tool: str,
    result: Mapping[str, Any],
    field: str,
    required_fields: Mapping[str, type | tuple[type, ...]],
) -> ValidatedToolResult:
    """Validate a list field containing typed object items."""
    items = result.get(field)
    if not isinstance(items, list):
        return _validation_failure(
            tool,
            "invalid_result_field",
            f"{tool} returned invalid {field!r}; expected list.",
            {"field": field, "received_type": type(items).__name__},
        )

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return _validation_failure(
                tool,
                "invalid_result_item",
                f"{tool} returned invalid {field}[{index}]; expected object.",
                {"field": field, "index": index, "received_type": type(item).__name__},
            )
        field_error = _validate_fields(tool, item, required_fields, context=f"{field}[{index}]")
        if field_error:
            return ValidatedToolResult(tool=tool, error=field_error)
    return ValidatedToolResult(tool=tool, data=dict(result))


def compact_tool_result(
    result: Any,
    *,
    tool: str | None = None,
    ok: bool | None = None,
    max_items: int = 8,
    max_text: int = 500,
) -> Any:
    """Compact a tool result for durable ARC provenance."""
    normalized = normalize_tool_result(result, tool=tool)
    if isinstance(normalized, MappingABC) and "error" in normalized:
        return {"ok": False, "error": normalize_tool_error(normalized["error"], tool=tool)}

    compacted = _compact_value(normalized, max_items=max_items, max_text=max_text, depth=0)
    if isinstance(compacted, dict):
        if ok is not None:
            compacted.setdefault("ok", bool(ok))
        return compacted
    return {"ok": bool(ok) if ok is not None else tool_result_ok(normalized), "value": compacted}


def format_tool_error(error: Any) -> str:
    """Format structured tool errors for user-facing expert answers."""
    normalized = normalize_tool_error(error)
    message = normalized.get("message") or str(error)
    next_action = normalized.get("next_action")
    if next_action:
        return f"{message} Next action: {next_action}"
    return str(message)


def _validation_failure(
    tool: str,
    code: str,
    message: str,
    details: Mapping[str, Any],
) -> ValidatedToolResult:
    return ValidatedToolResult(
        tool=tool,
        error=normalize_tool_error(
            {"type": "tool_contract", "code": code, "message": message},
            tool=tool,
            code=code,
            next_action="Fix the tool contract or inspect the tool backend before retrying.",
            details=details,
        ),
    )


def _validate_fields(
    tool: str,
    data: Mapping[str, Any],
    required_fields: Mapping[str, type | tuple[type, ...]],
    *,
    context: str = "result",
) -> dict[str, Any] | None:
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    for field_name, expected_type in required_fields.items():
        if field_name not in data:
            missing.append(field_name)
            continue
        value = data[field_name]
        if not _matches_type(value, expected_type):
            invalid.append(
                {
                    "field": field_name,
                    "expected": _type_label(expected_type),
                    "received": type(value).__name__,
                }
            )

    if not missing and not invalid:
        return None

    details: dict[str, Any] = {"context": context}
    if missing:
        details["missing"] = missing
    if invalid:
        details["invalid"] = invalid
    return normalize_tool_error(
        {
            "type": "tool_contract",
            "code": "invalid_result_shape",
            "message": f"{tool} returned an invalid {context} shape.",
        },
        tool=tool,
        code="invalid_result_shape",
        next_action="Fix the tool contract or inspect the tool backend before retrying.",
        details=details,
    )


def _matches_type(value: Any, expected_type: type | tuple[type, ...]) -> bool:
    expected = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    for item in expected:
        if item is int and isinstance(value, bool):
            continue
        if item is float and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, item):
            return True
    return False


def _type_label(expected_type: type | tuple[type, ...]) -> str:
    expected = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    return " | ".join(item.__name__ for item in expected)


def _compact_value(value: Any, *, max_items: int, max_text: int, depth: int) -> Any:
    if depth >= 4:
        return _compact_scalar(value, max_text=max_text)
    if isinstance(value, MappingABC):
        return {
            str(key): _compact_value(val, max_items=max_items, max_text=max_text, depth=depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        payload: dict[str, Any] = {
            "count": len(items),
            "items": [
                _compact_value(item, max_items=max_items, max_text=max_text, depth=depth + 1)
                for item in items[:max_items]
            ],
        }
        if len(items) > max_items:
            payload["truncated"] = True
        return payload
    return _compact_scalar(value, max_text=max_text)


def _compact_scalar(value: Any, *, max_text: int) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value)
    if len(text) > max_text:
        return text[:max_text] + "...[truncated]"
    return text


def _json_safe(value: Any) -> Any:
    if isinstance(value, MappingABC):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_json_error_text(text: str) -> Any | None:
    stripped = text.removeprefix("Error:").strip()
    if not stripped.startswith("{"):
        return None
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, MappingABC):
        return decoded
    return None
