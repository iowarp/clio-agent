"""Capability + metrics catalog leaf for the GACT server (#714 decomposition).

This module owns the static, client-renderable catalogs that the read-only
system routes project, plus the percentile helper the ``/v1/metrics`` envelope
builds from recorded tool-call durations. It is deliberately a *leaf*: it imports
only stdlib plus the dependency-free :mod:`clio_agent.optimizer.stub` (and the
wire ``MetricsLatencyStat`` lazily, inside the one function that needs it), and
has **zero** ``app.state`` coupling.

Two consumers share these definitions, so they live here as the single source:

* :mod:`clio_agent.gact.routes.system` -- ``GET /v1/capabilities`` /
  ``GET /v1/capability-gaps`` / ``GET /v1/metrics`` project them onto the wire.
* :mod:`clio_agent.gact.app` -- the message-turn streaming path reads
  :data:`_STREAM_FALLBACK_REASON_DEFINITIONS` (via ``_stream_fallback_payload`` /
  ``_record_stream_fallback``) to stamp why a turn fell back from live streaming.

Responsibilities:

* :data:`_STREAM_FALLBACK_REASON_DEFINITIONS` -- the audited catalog of reasons a
  turn delivered text through the synthetic/blocking path instead of live tokens.
* :func:`_stream_fallback_reason_capabilities` -- project that catalog for the
  ``x_clio_stream_fallback_reasons`` capability flag.
* :data:`_CAPABILITY_GAP_DEFINITIONS` + :func:`_capability_gap_metadata` -- the
  intentionally-unsupported / future-capability rows surfaced so clients can keep
  "not supported yet" affordances visible without inferring from missing routes.
* :func:`_latency_stat` -- nearest-rank count/p50/p95/max over latency samples.
"""

from __future__ import annotations

from typing import Any

from clio_agent.errors import (
    MCP_CAPABILITY_REFUSED,
    MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED,
    MCP_PROTOCOL_REFUSED,
    MCP_RESULT_DOWNGRADED_TO_COMPLETE,
    MCP_WIRE_CANCELLATION_UNAVAILABLE,
)
from clio_agent.optimizer.stub import (
    OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
    OPTIMIZER_NOT_IMPLEMENTED_REASON,
    OPTIMIZER_TRACKING_ISSUE,
)

_STREAM_FALLBACK_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "native_model_inputs_dropped": {
        "category": "capability_gap",
        # The turn still streamed live; what degraded is the INPUT, not the
        # delivery channel -- so this reason is honest about both.
        "synthetic_posthoc": False,
        "live_streaming": True,
        "recovery_actions": [
            "select_an_agent_whose_forward_accepts_images",
            "remove_the_attachment",
            "retry",
        ],
        "description": (
            "The turn carried native image/PDF model inputs but the executing agent's "
            "forward accepts no such parameter, so they were not delivered to the model. "
            "The text still streamed; the attachment did not ride the request."
        ),
    },
    "sync_execution_path": {
        "category": "non_streamed_execution",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_without_live_streaming", "reconfigure"],
        "description": "The answer arrived as one batch part: no live answer deltas this turn.",
    },
    MCP_RESULT_DOWNGRADED_TO_COMPLETE: {
        "category": "mcp_result_tolerance",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_with_complete_result", "upgrade_mcp_server"],
        "description": (
            "An MCP result explicitly carried a resultType this tasks-off client does not "
            "support, so it was downgraded to complete. An absent resultType is normal "
            "completeness and does not emit this reason."
        ),
    },
    MCP_CAPABILITY_REFUSED: {
        "category": "mcp_capability_refusal",
        "json_rpc_code": -32021,
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["enable_required_client_capability", "retry"],
        "description": "The MCP server refused a request requiring an absent client capability.",
    },
    MCP_PROTOCOL_REFUSED: {
        "category": "mcp_protocol_refusal",
        "json_rpc_code": -32022,
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["negotiate_supported_protocol_version", "retry"],
        "description": "The MCP server refused the negotiated protocol version.",
    },
    MCP_WIRE_CANCELLATION_UNAVAILABLE: {
        "category": "mcp_transport_limitation",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["continue_with_cooperative_cancellation", "upgrade_mcp_transport"],
        "description": (
            "The MCP transport did not settle after its in-flight call task was cancelled, "
            "so CLIO surfaced typed cooperative cancellation without claiming the server stopped."
        ),
    },
    MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED: {
        "category": "mcp_result_tolerance",
        "synthetic_posthoc": True,
        "live_streaming": False,
        "recovery_actions": ["raise_input_required_max_rounds", "fix_mcp_server", "retry"],
        "description": (
            "The modern-era MRTR loop (InputRequiredResult -> retry with inputResponses) "
            "exceeded its config-resolved tools.mcp.input_required_max_rounds bound without "
            "reaching a terminal result; the tool call surfaced a typed degrade."
        ),
    },
}


def _stream_fallback_reason_capabilities() -> dict[str, dict[str, Any]]:
    """Return the audited stream fallback reason catalog for capability metadata."""

    return {
        reason: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for reason, details in _STREAM_FALLBACK_REASON_DEFINITIONS.items()
    }


_CAPABILITY_GAP_DEFINITIONS: dict[str, dict[str, Any]] = {
    "voice": {
        "status": "unsupported",
        "advertised": False,
        "category": "future_capability",
        "description": (
            "Voice input/output is reserved for future CLIO work and is not "
            "wired to audio capture, transcription, or playback today."
        ),
        "client_behavior": "render_disabled",
        "recovery_actions": ["use_text_input", "hide_or_disable_voice_controls"],
        "related_endpoints": ["/v1/sessions/{sid}/voice/transcribe"],
    },
    "lsp": {
        "status": "unsupported",
        "advertised": False,
        "category": "future_capability",
        "description": (
            "Language-server integration is outside the current CLIO GACT "
            "surface; file and diff workflows are available instead."
        ),
        "client_behavior": "render_disabled",
        "recovery_actions": ["use_files_and_diffs", "hide_or_disable_lsp_controls"],
        "related_endpoints": ["/v1/lsp/*"],
    },
    "optimizer_command": {
        # #801: SPEC §3.3.1 capability-gap row — /optimize stays advertised as
        # a planned research surface while carrying the uniform structured
        # not-implemented stub (reason code + #633 pointer).
        "status": "unavailable",
        "advertised": True,
        "category": "deferred_command",
        "reason": OPTIMIZER_NOT_IMPLEMENTED_REASON,
        "tracking_issue": OPTIMIZER_TRACKING_ISSUE,
        "description": OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
        "client_behavior": "render_disabled",
        "recovery_actions": [
            "render_optimize_disabled",
            "retry_after_optimizer_support_lands",
        ],
        "related_commands": ["/optimize"],
        "related_endpoints": ["/v1/sessions/{sid}/commands/optimize"],
    },
}


def _capability_gap_metadata() -> dict[str, dict[str, Any]]:
    """Return CLIO capability gaps as client-renderable metadata."""

    return {
        name: {
            key: list(value) if isinstance(value, list) else value for key, value in details.items()
        }
        for name, details in _CAPABILITY_GAP_DEFINITIONS.items()
    }


def _latency_stat(samples: list[float]) -> Any:
    """Build a ``MetricsLatencyStat`` (count/p50/p95/max) from latency samples.

    Nearest-rank percentiles over the positive samples; empty -> all-zero stat.
    Used to populate ``GET /v1/metrics.latencies`` from real recorded tool-call
    durations (iowarp/clio-agent#655) so the TUI's live-profiling gate sees a
    non-empty signal on a real backend instead of always ``{}``."""
    from clio_agent.gact.types import MetricsLatencyStat  # noqa: PLC0415

    vals = sorted(float(s) for s in samples if isinstance(s, (int, float)) and s > 0)
    if not vals:
        return MetricsLatencyStat()

    def pct(p: float) -> float:
        if len(vals) == 1:
            return vals[0]
        idx = min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1))))
        return vals[idx]

    return MetricsLatencyStat(count=len(vals), p50_ms=pct(50), p95_ms=pct(95), max_ms=vals[-1])
