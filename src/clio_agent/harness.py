"""Core orchestration contracts for the CLIO agent harness."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from clio_agent.arc.schema import ToolCall

RouteTarget = Literal["chat", "data", "analysis", "visualization", "none"]
RouteSource = Literal["deterministic", "dspy", "guard"]
ExpertSource = Literal["deterministic", "dspy", "fallback"]

FILE_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|\.{1,2}/)?[^\s'\"`]+?\.(?:h5|hdf5|parquet|csv))",
    re.IGNORECASE,
)

ROUTE_TARGETS: tuple[RouteTarget, ...] = (
    "chat",
    "data",
    "analysis",
    "visualization",
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
    def from_dspy(cls, raw_target: Any) -> "RouteDecision":
        """Normalize and validate a DSPy router output."""
        target = str(raw_target or "").strip().lower()
        if target in ROUTE_TARGETS:
            return cls(
                target=target,  # type: ignore[arg-type]
                source="dspy",
                reason="DSPy router selected a valid CLIO route.",
                confidence=0.7,
            )
        return cls(
            target="chat",
            source="guard",
            reason=f"Router produced invalid target {target!r}; kept control in chat.",
            confidence=0.0,
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
            result=self.result,
            duration_ms=self.duration_ms,
            cached=False,
        )


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


@dataclass
class RunTrace:
    """Per-request execution trace used by the harness and ARC."""

    route: RouteDecision
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.time)
    tools: list[ToolObservation] = field(default_factory=list)

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

    @property
    def duration_ms(self) -> float:
        """Elapsed wall-clock duration for this trace."""
        return (time.time() - self.started_at) * 1000


class IntentRouter:
    """Deterministic CLIO router for obvious scientific-data intents.

    LLM routing remains useful for vague language, but explicit file/tool
    intents should not depend on model parsing.
    """

    _VISUAL_TOKENS = ("plot", "chart", "graph", "histogram", "scatter", "visualize")
    _HDF5_TOKENS = (".h5", ".hdf5", "hdf5", "chunking", "compression")
    _PARQUET_TOKENS = (".parquet", "parquet", "schema", "statistics", "null count")
    _CSV_TOKENS = (".csv", "csv")
    _CHAT_TOKENS = ("who are you", "what can you do", "help")
    _GREETINGS = {"hi", "hello", "hey"}

    def classify(self, question: str) -> RouteDecision | None:
        """Return a deterministic route when the intent is unambiguous."""
        q = question.lower().strip()
        capabilities: list[str] = []

        if any(token in q for token in self._VISUAL_TOKENS):
            capabilities.append("visualization")
            return RouteDecision(
                target="visualization",
                source="deterministic",
                reason="Visualization verb detected.",
                confidence=0.95,
                capabilities=tuple(capabilities),
            )

        if any(token in q for token in self._HDF5_TOKENS):
            capabilities.extend(["hdf5", "data-layout"])
            return RouteDecision(
                target="data",
                source="deterministic",
                reason="HDF5 or storage-layout intent detected.",
                confidence=0.95,
                capabilities=tuple(capabilities),
            )

        if any(token in q for token in self._PARQUET_TOKENS):
            capabilities.extend(["parquet", "profiling"])
            return RouteDecision(
                target="analysis",
                source="deterministic",
                reason="Parquet/statistics intent detected.",
                confidence=0.95,
                capabilities=tuple(capabilities),
            )

        if any(token in q for token in self._CSV_TOKENS):
            capabilities.extend(["csv", "profiling"])
            return RouteDecision(
                target="analysis",
                source="deterministic",
                reason="CSV inspection intent detected.",
                confidence=0.9,
                capabilities=tuple(capabilities),
            )

        if q in self._GREETINGS or any(token in q for token in self._CHAT_TOKENS):
            return RouteDecision(
                target="chat",
                source="deterministic",
                reason="Conversational CLIO intent detected.",
                confidence=0.9,
            )

        return None


def tool_result_ok(result: Any) -> bool:
    """Return whether a tool result represents success."""
    if isinstance(result, dict) and "error" in result:
        return False
    if isinstance(result, str) and result.startswith("Error:"):
        return False
    return True


def extract_file_paths(question: str, file_context: str, suffixes: set[str]) -> list[Path]:
    """Extract file paths with one of the requested suffixes.

    Paths explicitly provided in the user question are kept even if they do not
    exist so tools can return the policy or file-read error. Paths recovered
    from ARC file context must still exist, avoiding stale profile paths.
    """
    paths: list[Path] = []
    seen: set[str] = set()

    def add_matches(text: str, *, include_missing: bool) -> None:
        for match in FILE_PATH_RE.finditer(text):
            raw = match.group("path").rstrip(".,;:)]}")
            path = Path(raw).expanduser()
            if path.suffix.lower() not in suffixes:
                continue
            if not path.is_absolute():
                path = path.resolve()
            if not include_missing and not path.exists():
                continue
            key = str(path)
            if key not in seen:
                paths.append(path)
                seen.add(key)

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


def format_tool_error(error: Any) -> str:
    """Format structured tool errors for user-facing expert answers."""
    if isinstance(error, dict):
        message = error.get("message") or str(error)
        next_action = error.get("next_action")
        if next_action:
            return f"{message} Next action: {next_action}"
        return str(message)
    return str(error)
