"""Research-grade semantic execution events for GACT.

Semantic events are higher-level than the existing wire events. A
``message.part.delta`` tells the TUI that text arrived; a semantic event
tells a researcher that CLIO started a turn, called a tool, delegated to
an expert, accessed memory, or settled a turn. The same event object feeds
live SSE, durable trace logging, and user hooks.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from clio_agent.arc.segments import _encode_safe
from clio_agent.gact.events import Event, EventBus

SCHEMA_VERSION = "clio.semantic_event.v1"
DEFAULT_DETAIL_LEVEL = "semantic"
DETAIL_LEVELS = {"off", "metadata", "semantic", "full_debug"}
REDACTED_VALUE = "[redacted]"
# CLIO does NOT redact its own trajectory from the user's own session: the SSE
# stream carries the full content so a generic client renders the real session
# (same principle as the durable trace/ARC). Only GENUINE SECRETS — credentials
# that are never session content — are redacted. Everything else (formerly here:
# text, input, question, prompt, reasoning, reasoning_content, rendered_*, result,
# response, content, args, arguments, raw, trajectory, transcript, final_message,
# new_content, output) now passes through.
SENSITIVE_KEYS = {
    "api_key",
    "access_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "token",
}

# Per-EVENT SSE allow-list: keys that are normally redacted (in SENSITIVE_KEYS)
# but are legitimate live content for THESE specific event types, so the SSE
# projection keeps them. This is event/field scoped ON PURPOSE: e.g. ``reasoning``
# is the model's chain-of-thought, which the TUI wants on the step event, but it
# must STAY redacted on lm.call / lm.token.delta / raw-prompt events (where it is
# bulky/streamed/duplicated). Scoping here avoids globally un-redacting a key.
SSE_KEEP_KEYS_BY_EVENT: dict[str, frozenset[str]] = {
    "react.step.completed": frozenset({"reasoning"}),
    "expert.extract.completed": frozenset({"reasoning"}),
}

# UI/SSE serving allow-list: the ONLY semantic-event types that reach the live
# bus (GET /v1/sessions/{sid}/events) — the ReAct trajectory the TUI renders.
# Everything else stays off the served wire but is captured FULL on the durable
# trace + ARC (a serving-layer filter, never a capture filter). Any FAILED/ERROR
# event passes regardless, EXCEPT ``SSE_TRACE_ONLY_EVENT_TYPES`` (unconditional).
# See the four ReAct atoms:
#   a) delegation  = blueprint.delegation.* + the orchestrator's reasoning
#                    (carried on expert.response.completed for CoT orchestrators)
#   b) tool call   } react.step.completed (thought + tool_name + tool_args
#   c) tool result }                       + observation), for ReAct leaves
#   d) extract     = expert.extract.completed (output + structured workflow_state)
SSE_UI_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "react.step.completed",
        # Routing decisions are OBSERVABILITY events (the prototype's timeline
        # "routing_decision" rows), never transcript parts (clean-wire rule).
        "routing.decision",
        "expert.extract.completed",
        "expert.response.completed",
        "expert.lifecycle.started",
        # The delegation atom rides one of two prefixes depending on the runtime:
        # ``blueprint.delegation.*`` for Agent Blueprint experts and the plain
        # ``delegation.*`` for expert-pack / prompt-agent delegations. Both are the
        # SAME atom and both must reach the UI (``.failed`` also passes via the
        # always-status gate, but is listed for explicitness).
        "blueprint.delegation.started",
        "blueprint.delegation.completed",
        "blueprint.delegation.parent_resumed",
        "blueprint.delegation.failed",
        "delegation.started",
        "delegation.completed",
        "delegation.parent_resumed",
        "delegation.failed",
        # Memory search is an agent ACTION the user opts into (a retrieval step in
        # the trajectory), not lifecycle bookkeeping -- surface its result live.
        "memory.search.completed",
        # A skill load is an agent ACTION with provenance the UI renders
        # (#920; gact-tui#315): which procedure the expert pulled in, from where.
        "skill.loaded",
        # Artifacts (#966 S2 / #968): a generated output gains outbound wire
        # identity. ``artifact.created`` fires per new immutable version;
        # ``artifact.version.added`` / ``artifact.alias.moved`` are the version-chain
        # + alias atoms (emit sites land in S4 — allow-listed now so the wire is
        # ready). ``artifact.used`` / ``artifact.transform.recorded`` deliberately
        # STAY trace-only (provenance substrate the UI does not render). All are
        # captured FULL on the durable trace + ARC regardless; this only gates
        # serving. Redaction is the same ``semantic`` detail path every UI event
        # uses — an artifact record carries no ``SENSITIVE_KEYS`` field, so the SSE
        # projection is the full record minus (absent) credentials.
        "artifact.created",
        "artifact.version.added",
        "artifact.alias.moved",
        # Document artifacts: protocol-specific UI atoms, never inferred.
        "document.review.created",
        "document.review.dispatched",
        "document.native_comment.imported",
        "document.working_copy.changed",
        "document.working_copy.conflict",
        # Grants on the record (B5 #979): every effective-boundary change is a
        # user/model DECISION the TUI renders — a workspace/session write-root grant
        # or revoke (``boundary.granted``/``boundary.revoked``, ``kind: root|domain``)
        # and the permission request/resolution lifecycle around it. Unlike the
        # trace-only provenance substrate below (net.egress / policy_violation), these
        # ARE UI events — the user must see the prompt and the resulting grant live.
        # ``permission.requested`` was emitted as a semantic event (turn.py) but never
        # SSE-listed, while ``permission.resolved`` was bus-only: both are now listed
        # here so the request→resolution lifecycle is consistently served (B5 #979.8).
        "boundary.granted",
        "boundary.revoked",
        "permission.requested",
        "permission.resolved",
    }
)

# Provenance SUBSTRATE the UI never renders — definitionally trace-only, even on
# failure. These artifact atoms are captured FULL on the durable trace + ARC and
# served via the /transforms and /lineage ROUTES, but must NEVER ride the live SSE
# wire. Unlike a failed *action* (a tool/delegation/expert step), a failed or
# contended provenance record is still substrate, not a user-facing error — so the
# ``_SSE_ALWAYS_STATUSES`` override below must NOT lift them onto the wire. Without
# this exclusion a transform whose status is "failed" (e.g. a contended record that
# generated 0 outputs) would leak onto SSE non-deterministically — exactly the
# data-dependent clean-stream violation this set closes (S5 gate3 C5).
SSE_TRACE_ONLY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # P2.7 hook audit (#1075): one ``hook.invoked`` per hook invocation — the
        # governance audit (decision/denial/error/pre-exec rejection). Durable-trace +
        # ARC substrate the operator queries after the fact, NEVER a UI row; declared
        # trace-only so no status can lift it onto the served wire.
        "hook.invoked",
        # Stall-triggered replanning (P1.6d #1068): the leaky-bucket score change per turn and the
        # threshold suggestion are governance substrate the operator queries after the fact — never a
        # UI atom (the suggestion reaches the model via a per-turn-input attachment, not an SSE row).
        "replan.stall_scored",
        "replan.suggested",
        "artifact.used",
        "artifact.enriched",  # A9 (#1176) dedup-enrichment side index — substrate, not a UI row.
        "artifact.transform.recorded",
        "artifact.transform.failed",
        "artifact.proposed",
        # CAS budget housekeeping (S6 #972): an evicted blob / a swept crash-orphaned
        # temp is durable-trace substrate the UI never renders — never on the SSE wire.
        "artifact.cas.evicted",
        "artifact.cas.tmp_swept",
        # Sandbox boot conformance (B1 #975): the resolved write-confinement mechanism
        # is durable-trace substrate (the conformance floor), never a UI row — declared
        # trace-only here so a future non-"completed" emit cannot ride the
        # ``_SSE_ALWAYS_STATUSES`` override onto the wire.
        "sandbox.state",
        # Policy violations (B2 #976): a fence-denied (or fence-escaping) out-of-root write
        # is durable-trace provenance substrate — the typed ``policy_violation`` node that
        # replaces #966's ``gap``. B5 (#979) confirmed it STAYS trace-only (the SPEC rider
        # lists only ``boundary.*`` + ``permission.*`` as UI events; the grant AFFORDANCE the
        # model acts on rides the node's ``next_action`` field, not an SSE row). Declared here
        # so its "failed"-status emit cannot ride ``_SSE_ALWAYS_STATUSES`` onto the UI wire.
        "artifact.policy_violation",
        # Network egress (B4 #978): every forwarded child connection through the clio
        # chokepoint is a ``net.egress`` record — high-volume, durable-only provenance
        # substrate that feeds the ``used web:domain@time`` ingest edge. B5 (#979) confirmed
        # it STAYS trace-only (the UI renders the ``boundary.granted{kind: domain}`` a deny-mode
        # grant produces, never the raw per-connection egress); declared trace-only so no
        # status can lift it onto the UI stream.
        "net.egress",
        # Built-toolset inventory (obs Tools tab "called | available" toggle):
        # one ``agent.toolset.recorded`` per built react expert, read back by
        # GET /v1/sessions/{sid}/trace for the "available" view. Durable-trace
        # substrate only -- never a live SSE row (the tab reads the trace poll,
        # not the event stream) -- declared here so no future non-"completed"
        # status can lift it onto the wire via ``_SSE_ALWAYS_STATUSES``.
        "agent.toolset.recorded",
        "provider.thinking.redacted",  # no-silent-fallback CoT-redaction reason: trace-only
    }
)

# Statuses that ALWAYS reach the UI wire regardless of event_type — a failure or
# cancellation must never be filtered out of the served stream. This override is
# for *actions/lifecycle* (a failed tool call / delegation / expert step that is
# not otherwise allow-listed); it deliberately does NOT apply to the trace-only
# provenance substrate above (which is excluded first, unconditionally).
_SSE_ALWAYS_STATUSES: frozenset[str] = frozenset({"failed", "error", "cancelled"})


def event_reaches_ui(event_type: str, status: str = "") -> bool:
    """True when a semantic event should be published to the live UI bus.

    The atom allow-list plus an unconditional pass for failure/cancellation
    statuses — but trace-only provenance substrate is excluded FIRST, so no status
    can lift it onto the wire. Capture (durable trace + ARC) is unaffected — this
    gates serving only.
    """
    if event_type in SSE_TRACE_ONLY_EVENT_TYPES:
        return False
    return (
        event_type in SSE_UI_EVENT_TYPES or (status or "").strip().lower() in _SSE_ALWAYS_STATUSES
    )


# The "body" fields of a SemanticEvent — these carry the rich, potentially
# sensitive payloads that are captured in FULL durably but projected/redacted
# for SSE and hooks. The envelope fields (ids, status, summary, timestamps) are
# never redacted.
_BODY_FIELDS = ("actor", "subject", "blueprint", "provider", "payload")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_id() -> str:
    return f"sem_{uuid.uuid4().hex[:16]}"


def _json_safe(value: Any) -> Any:
    """Coerce ``value`` to a plain JSON/msgpack-native form for the durable trace,
    the SSE projection, and hook views.

    Delegates to the ONE shared coercion :func:`clio_agent.arc.segments._encode_safe`
    (re-exported as :func:`clio_agent.arc.live._encode_safe`) — the SAME function
    :func:`clio_agent.arc.live.build_event_content` uses to encode the canonical
    ``_events`` log. Sharing the coercion is what makes the durable-trace JSONL body a
    LOSSLESS derivation of the ``_events`` segment body (design §2.8.b caveat a /
    §4.3, #737 S1): the trace line is the log with the same bytes, not a second,
    differently-encoded history.

    Before unification this function had its own rules — ``sorted`` sets (which raised
    ``TypeError`` on a mixed-type set), ``str()`` for frozensets / dataclasses / plain
    objects (leaking a nondeterministic ``<obj at 0x…>`` memory address), and
    ``exclude_none`` pydantic dumps — every one of which DIVERGED from the log encoder,
    so no projection could reconstruct one from the other. The shared coercion is a
    strict superset (frozenset→list, dataclass→asdict, ``__dict__``→dict, None kept) and
    never raises, so it is both lossless-derivable and more robust.
    """
    return _encode_safe(value)


def normalize_detail_level(value: str) -> str:
    normalized = (value or DEFAULT_DETAIL_LEVEL).strip().lower()
    return normalized if normalized in DETAIL_LEVELS else DEFAULT_DETAIL_LEVEL


def _redact_value(value: Any) -> Any:
    if value in (None, "", [], {}):
        return value
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return f"{REDACTED_VALUE}:{len(value)} chars"
    return REDACTED_VALUE


def _semantic_safe(value: Any, allow: frozenset[str] = frozenset()) -> Any:
    value = _json_safe(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            # ``allow`` is the per-event SSE allow-list (SSE_KEEP_KEYS_BY_EVENT):
            # a key that is normally sensitive but is legitimate live content for
            # THIS event type survives unredacted.
            if key_s.lower() in SENSITIVE_KEYS and key_s.lower() not in allow:
                result[key_s] = _redact_value(item)
            else:
                result[key_s] = _semantic_safe(item, allow)
        return result
    if isinstance(value, list):
        return [_semantic_safe(item, allow) for item in value]
    return value


def _payload_for_detail(
    value: dict[str, Any], detail_level: str, allow: frozenset[str] = frozenset()
) -> dict[str, Any]:
    detail_level = normalize_detail_level(detail_level)
    if detail_level in {"off", "metadata"}:
        return {}
    if detail_level == "semantic":
        return _semantic_safe(value, allow)
    return _json_safe(value)


@dataclass
class SemanticEvent:
    """Single semantic observation from a CLIO run."""

    event_type: str
    session_id: str
    trace_id: str
    turn_id: str = ""
    workspace_id: str = ""
    span_id: str = field(default_factory=_event_id)
    parent_span_id: str = ""
    status: str = "completed"
    summary: str = ""
    actor: dict[str, Any] = field(default_factory=dict)
    subject: dict[str, Any] = field(default_factory=dict)
    blueprint: dict[str, Any] = field(default_factory=dict)
    provider: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    live_observed: bool = True
    detail_level: str = DEFAULT_DETAIL_LEVEL
    occurred_at: str = field(default_factory=_utcnow_iso)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self, projection: str = "full") -> dict[str, Any]:
        """Serialize the event at the requested projection.

        ``full`` (default) — every body field unredacted; this is what the
        DURABLE canonical trace and live consumers (ARC) receive. ``sse`` —
        body fields redacted per the event's ``detail_level`` (the only
        surviving redaction path; for SSE/UI). ``metadata``/``off`` — body
        fields emptied. The envelope (ids/status/summary/timestamps) is never
        redacted at any projection.
        """
        detail_level = normalize_detail_level(self.detail_level)
        envelope = {
            "schema_version": self.schema_version,
            "event_id": self.span_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "status": self.status,
            "summary": self.summary,
            "live_observed": self.live_observed,
            "detail_level": detail_level,
            "occurred_at": self.occurred_at,
        }
        if projection == "full":
            bodies = {
                field_name: _json_safe(getattr(self, field_name)) for field_name in _BODY_FIELDS
            }
        elif projection in ("metadata", "off", "none"):
            bodies = {field_name: {} for field_name in _BODY_FIELDS}
        else:  # "sse" (and any unknown) → honor the event's detail_level
            allow = SSE_KEEP_KEYS_BY_EVENT.get(self.event_type, frozenset())
            bodies = {
                field_name: _payload_for_detail(getattr(self, field_name), detail_level, allow)
                for field_name in _BODY_FIELDS
            }
        return {**envelope, **bodies}


# --- Projection registry -----------------------------------------------------
# One canonical event is captured at MAX fidelity; each consumer gets a
# projection. The durable trace + live consumers (ARC) take ``project_full``;
# SSE/hooks take a redacted view; handoff/history/research are explicit views.


def project_full(event: SemanticEvent) -> dict[str, Any]:
    """Unredacted view — durable canonical trace, live consumers, research."""
    return event.to_dict("full")


def project_sse(event: SemanticEvent) -> dict[str, Any]:
    """Redacted view honoring detail_level — for the live SSE/UI stream."""
    return event.to_dict("sse")


def project_hook(event: SemanticEvent, *, full: bool = False) -> dict[str, Any]:
    """View handed to user hooks. Redacted by default (hooks are user code)."""
    return event.to_dict("full" if full else "sse")


def project_research(event: SemanticEvent) -> dict[str, Any]:
    """Full view for research consumers (IO-prefetch, error detection)."""
    return event.to_dict("full")


def project_handoff(event: SemanticEvent, mode: str = "FINAL") -> dict[str, Any]:
    """Expert→parent handoff view (wired at the handoff seam in a later stage).

    ``FINAL`` keeps the answer + tool evidence + workflow_state but strips the
    heavy reasoning/trajectory; ``SUMMARY`` keeps only the answer. Reduction is
    a projection of the full event, never a capture-time loss.
    """
    full = event.to_dict("full")
    payload = dict(full.get("payload") or {})
    if mode.upper() == "SUMMARY":
        keep = {"answer", "result_summary"}
    else:  # FINAL
        keep = {"answer", "result_summary", "tools_called", "workflow_state", "evidence"}
    full["payload"] = {k: v for k, v in payload.items() if k in keep}
    return full


def project_history(event: SemanticEvent) -> dict[str, Any]:
    """dspy.History / KV-rehydration view — DEFERRED (see GitHub issue)."""
    raise NotImplementedError("dspy.History / resume projection is deferred")


# --- _events log → durable-trace derivation (design §2.8.a / §4.3; #737 S1) ---
# The ARC ``_events`` semantic-event log is the CANONICAL floor: every event is folded
# into ``_events`` (via ``build_event_content``) BEFORE the durable-trace sink writes
# its JSONL line (``to_dict("full")``). The seams below PIN that the trace is a
# *derivation* of the log, not an independent second history:
#   * ``semantic_event_from_events_content`` folds a stored ``_events`` content dict back
#     into a SemanticEvent so the trace line can be RE-DERIVED and diffed (this file).
#   * ``backfill_events_from_trace`` (``gact.trace_backfill``) is the INVERSE recovery
#     seam for the #762 case where ``release_session`` erases ``_events`` when
#     ``trace.backend=file``: the JSONL is then the lossless backfill source.

#: SemanticEvent envelope fields the ``_events`` content form (``build_event_content``)
#: does NOT carry. ``session_id``/``turn_id`` live on the ``Segment`` ENVELOPE (so a
#: fold recovers them from the segment, not the content); the rest are identity/serving
#: concerns a later slice's ``message_part`` atoms will carry (§2.3). Declared here so
#: the derivation proof masks EXACTLY these and asserts every carried field verbatim.
EVENTS_CONTENT_CARRIED_FIELDS: frozenset[str] = frozenset(
    {
        "event_type",
        "status",
        "summary",
        "actor",
        "subject",
        "payload",
        "provider",
        "occurred_at",
        "trace_id",
    }
)

#: The ``to_dict("full")`` keys NOT reconstructable from ``_events`` content alone
#: (``session_id``/``turn_id`` ARE, from the Segment envelope). A later slice adds the
#: identity-carrying atoms (§2.3); until then the derivation proof masks these.
EVENTS_CONTENT_UNCARRIED_TRACE_FIELDS: frozenset[str] = frozenset(
    {
        "event_id",
        "span_id",
        "parent_span_id",
        "workspace_id",
        "blueprint",
        "live_observed",
        "detail_level",
        "schema_version",
    }
)


def semantic_event_from_events_content(
    content: dict[str, Any], *, session_id: str = "", turn_id: str = ""
) -> SemanticEvent:
    """Fold a stored ``_events`` segment ``content`` dict back into a SemanticEvent.

    ``content`` is exactly what :func:`clio_agent.arc.live.build_event_content` stored
    (the carried fields, :data:`EVENTS_CONTENT_CARRIED_FIELDS`); ``session_id`` and
    ``turn_id`` come from the owning ``Segment`` envelope (the caller passes them). The
    fields ``_events`` does not carry (:data:`EVENTS_CONTENT_UNCARRIED_TRACE_FIELDS`)
    take deterministic empties — in particular ``span_id=""`` so the fold NEVER mints a
    random id — which is why ``to_dict("full")`` on the result matches the durable trace
    line on every carried field and only differs on the (declared-uncarried) envelope.

    Args:
        content: The ``_events`` segment content (``build_event_content`` form).
        session_id: The owning segment's ``session_id`` (envelope, not content).
        turn_id: The owning segment's ``turn_id`` (envelope, not content).

    Returns:
        A SemanticEvent whose carried fields equal ``content``'s, with deterministic
        empty defaults for the uncarried envelope so the fold is pure (no random ids).
    """
    return SemanticEvent(
        event_type=str(content.get("event_type", "") or ""),
        session_id=session_id,
        trace_id=str(content.get("trace_id", "") or ""),
        turn_id=turn_id,
        span_id="",  # deterministic: never mint a random id on a fold
        status=str(content.get("status", "") or ""),
        summary=str(content.get("summary", "") or ""),
        actor=dict(content.get("actor") or {}),
        subject=dict(content.get("subject") or {}),
        provider=dict(content.get("provider") or {}),
        payload=dict(content.get("payload") or {}),
        occurred_at=str(content.get("occurred_at", "") or ""),
    )


def trace_line_from_events_content(
    content: dict[str, Any], *, session_id: str = "", turn_id: str = ""
) -> dict[str, Any]:
    """Re-derive a durable-trace JSONL line (``to_dict("full")`` shape) from ``_events``
    content — the projection scope-1 proves equals the trace the sink actually wrote,
    masking :data:`EVENTS_CONTENT_UNCARRIED_TRACE_FIELDS`.
    """
    event = semantic_event_from_events_content(content, session_id=session_id, turn_id=turn_id)
    return event.to_dict("full")


# --- lm.token.delta: the live token stream on the highway (#693) --------------
# The single LM-stream tap emits ``lm.token.delta`` events so the live token
# stream rides the SAME highway as every other semantic event (one capture, N
# projections) instead of being read ad-hoc by streamify + the watchdog drain.

LM_TOKEN_DELTA = "lm.token.delta"


def lm_token_delta_payload(
    *,
    content: str = "",
    reasoning: str = "",
    field: str = "answer",
) -> dict[str, Any]:
    """Build the payload for an ``lm.token.delta`` so ONE event feeds every
    consumer correctly through the existing projection rules:

    - user-facing answer text goes under ``delta`` — deliberately NOT a
      ``SENSITIVE_KEYS`` name — so it SURVIVES ``project_sse`` and reaches the
      live UI as the streaming answer chunk;
    - chain-of-thought goes under ``reasoning`` — which IS in ``SENSITIVE_KEYS``
      — so ``project_sse`` redacts it to an activity heartbeat (length only,
      refreshing the watchdog / "thinking" indicator) while ``project_full``
      (durable trace + ARC) keeps it verbatim.

    Categorization (session/turn/span/parent_span/actor=expert) rides the event
    envelope, set upstream at emit — the consumer never re-derives it.
    """
    payload: dict[str, Any] = {"field": field}
    if content:
        payload["delta"] = content
    if reasoning:
        payload["reasoning"] = reasoning
    return payload


class SemanticTraceBackend(Protocol):
    """Durable sink for semantic events."""

    name: str

    def emit(self, event: SemanticEvent) -> None: ...


class NoopSemanticTraceBackend:
    name = "none"

    def emit(self, event: SemanticEvent) -> None:
        return


# ONE process-global writer thread drains ALL FileSemanticTraceBackend instances.
# Why global + started at backend CONSTRUCTION (not per-emit): starting a thread
# from the event-loop thread DURING a turn cancels the turn task under the
# anyio/TestClient portal; constructing the backend happens at build_app (off the
# turn loop), so the thread is created safely once. A single shared thread also
# avoids one-thread-per-app (the trace is on by default) blowing up under tests.
_TRACE_WRITE_QUEUE: "queue.Queue[tuple[Path, SemanticEvent] | None]" = queue.Queue()
_TRACE_WRITER_THREAD: threading.Thread | None = None
_TRACE_WRITER_LOCK = threading.Lock()


def _trace_writer_loop() -> None:
    while True:
        item = _TRACE_WRITE_QUEUE.get()
        try:
            if item is None:  # wake/no-op; the shared writer is never stopped
                continue
            path, event = item
            try:
                # Serialize HERE (off the turn loop): json.dumps of a full event
                # (reasoning + tool results) is non-trivial CPU; doing it on the
                # caller's event-loop thread destabilizes turns under the portal.
                line = json.dumps(event.to_dict("full"), sort_keys=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
                    f.write("\n")
            except Exception as exc:  # noqa: BLE001 - a write error must not kill the writer
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event("TRACE-WRITE", "durable trace write failed (event dropped): %r", exc)
        finally:
            _TRACE_WRITE_QUEUE.task_done()


def _ensure_trace_writer() -> None:
    """Start the shared writer once, OFF the turn event loop (backend init time)."""
    global _TRACE_WRITER_THREAD  # noqa: PLW0603
    if _TRACE_WRITER_THREAD is not None:
        return
    with _TRACE_WRITER_LOCK:
        if _TRACE_WRITER_THREAD is None:
            thread = threading.Thread(
                target=_trace_writer_loop, name="SemanticTraceWriter", daemon=True
            )
            thread.start()
            _TRACE_WRITER_THREAD = thread


class FileSemanticTraceBackend:
    """Append semantic events as JSONL, written OFF the calling thread.

    If ``path`` is a directory, events are split into
    ``<session_id>.semantic.jsonl`` files. If it is a file path, all
    events append to that file.

    ``emit`` serializes the FULL event on the caller (cheap CPU) then enqueues to
    the shared writer thread, so the turn event loop is never blocked by file I/O
    (the trace is ON by default). ``flush`` blocks until the queue drains
    (tests/readers); ``close`` drains too (the shared daemon writer lives for the
    process). The durable trace always captures the FULL event; redaction/capping
    is a per-consumer projection applied elsewhere, never here.
    """

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path
        _ensure_trace_writer()

    # A configured path is a single JSONL file ONLY when it carries a recognised
    # trace-file extension; otherwise it is a directory of per-session files.
    # Plain ``Path.suffix`` truthiness misfires on directory paths that contain
    # dots -- e.g. a model-named grind dir ".../trace_..._qwopus3.5-9b-v3_sandiego"
    # whose ``.suffix`` is ".5-9b-v3_sandiego" -- which made the writer try to open
    # a directory as a file and silently drop every event (empty trace).
    _FILE_SUFFIXES = frozenset({".jsonl", ".json", ".ndjson", ".log"})

    def _path_for(self, event: SemanticEvent) -> Path:
        if self.path.suffix.lower() in self._FILE_SUFFIXES:
            return self.path
        return self.path / f"{event.session_id}.semantic.jsonl"

    def emit(self, event: SemanticEvent) -> None:
        # Near-zero work on the caller (which may be the turn's event-loop thread):
        # just resolve the path + enqueue. Serialization + I/O happen in the writer.
        _TRACE_WRITE_QUEUE.put((self._path_for(event), event))

    def flush(self) -> None:
        """Block until all enqueued events have been written (tests/readers)."""
        _TRACE_WRITE_QUEUE.join()

    def close(self) -> None:
        """Drain pending writes (the shared daemon writer lives for the process)."""
        _TRACE_WRITE_QUEUE.join()


def build_trace_backend(default_root: Path) -> SemanticTraceBackend:
    """Build the configured durable trace backend.

    ``CLIO_SEMANTIC_TRACE_BACKEND`` accepts ``file``, ``factory``, or ``none``.
    ``CLIO_SEMANTIC_TRACE_PATH`` may point at either a JSONL file or a
    directory. The durable file backend (``FileSemanticTraceBackend``) writes
    OFF the turn event loop via a shared writer thread, so it is cheap and safe
    to enable. It is the single canonical record the memory underbelly stands on
    (ARC live view, re-extract repair, agent-to-agent handoff, error detection,
    research replay) and the grind/research runs enable it (``=file``).

    DEFAULT is still ``none`` (opt-in): flipping it on surfaced a separate, real
    turn-lifecycle fragility -- a turn runs as a request-loop background task
    (app.py create_task), and the extra writer-thread GIL load + mid-turn tool
    observer emits push it past the request-loop teardown window, cancelling the
    turn under the test harness. Making the durable trace the DEFAULT is gated on
    a turn-task-robustness fix (run turns off the request loop). Until then,
    enable explicitly. Live semantic SSE is independent and always on.
    """

    from clio_agent import conf

    backend = (
        conf.resolve(
            "trace.backend",
            env="CLIO_SEMANTIC_TRACE_BACKEND",
            default="none",
            cast=conf.as_str,
        )
        .strip()
        .lower()
    )
    if backend in {"", "none", "off", "disabled"}:
        return NoopSemanticTraceBackend()
    if backend == "file":
        raw_path = conf.resolve(
            "trace.path", env="CLIO_SEMANTIC_TRACE_PATH", default="", cast=conf.as_str
        ).strip()
        path = Path(raw_path).expanduser() if raw_path else default_root
        return FileSemanticTraceBackend(path)
    if backend in {"factory", "python_factory", "custom"}:
        factory_path = conf.resolve(
            "trace.semantic_factory",
            env="CLIO_SEMANTIC_TRACE_FACTORY",
            default="",
            cast=conf.as_str,
        ).strip()
        if not factory_path:
            raise ValueError(
                "CLIO_SEMANTIC_TRACE_FACTORY is required when CLIO_SEMANTIC_TRACE_BACKEND=factory"
            )
        factory = _load_factory(factory_path)
        raw_config = conf.resolve(
            "trace.semantic_config", env="CLIO_SEMANTIC_TRACE_CONFIG", default="", cast=conf.as_str
        ).strip()
        config = json.loads(raw_config) if raw_config else {}
        result = factory(default_root=default_root, config=config)
        if not callable(getattr(result, "emit", None)):
            raise TypeError("semantic trace factory must return an object with emit(event)")
        if not getattr(result, "name", ""):
            result.name = "factory"
        return result
    raise ValueError(f"unsupported semantic trace backend: {backend}")


def _load_factory(path: str) -> Any:
    module_name, sep, attr = path.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError("factory path must be 'module.submodule:function'")
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    if not callable(factory):
        raise TypeError(f"semantic trace factory is not callable: {path}")
    return factory


class SemanticEventSink:
    """Fan out semantic events to SSE, durable traces, and hooks."""

    def __init__(
        self,
        *,
        bus: EventBus,
        trace_backend: SemanticTraceBackend,
        detail_level: str = DEFAULT_DETAIL_LEVEL,
        capture: bool = True,
        hooks_full: bool = False,
        live_consumers: list[Callable[[SemanticEvent], None]] | None = None,
    ) -> None:
        self.bus = bus
        self.trace_backend = trace_backend
        self.detail_level = normalize_detail_level(detail_level)
        # ``capture`` gates the DURABLE canonical write (an SSE ``detail_level``
        # of "off" must NOT blind the canonical store — that is an SSE-only knob).
        self.capture = capture
        self.hooks_full = hooks_full
        self.live_consumers: list[Callable[[SemanticEvent], None]] = list(live_consumers or [])

    @property
    def trace_backend_name(self) -> str:
        return self.trace_backend.name

    def emit(self, event: SemanticEvent) -> dict[str, Any]:
        event.detail_level = normalize_detail_level(event.detail_level or self.detail_level)
        # Durable canonical store + live consumers (ARC) ALWAYS get the FULL
        # event, gated only on ``capture`` — never on detail_level. Projection /
        # redaction happens per-consumer below.
        if self.capture:
            self.trace_backend.emit(event)
        for consumer in self.live_consumers:
            try:
                consumer(event)  # raw SemanticEvent, pre-projection (ARC folds this)
            except Exception as exc:  # noqa: BLE001 - never crash a turn, but never silent
                from clio_agent.runtime import trace  # noqa: PLC0415

                trace.event("HIGHWAY-CONSUMER", "live consumer raised (event dropped): %r", exc)
        full = project_full(event)
        # SSE + hooks get projected (redacted) views; SSE honors "off".
        if event.detail_level != "off":
            # Serving gate: only the ReAct atoms (and any failure) reach the live
            # UI bus. The rest is substrate captured FULL above (trace + ARC) but
            # not rendered by the UI, so it stays off the served wire. Hooks still
            # see EVERY event (user observability code), independent of this gate.
            if event_reaches_ui(event.event_type, event.status):
                self.bus.publish(
                    Event(
                        type="semantic.event",
                        session_id=event.session_id,
                        payload=project_sse(event),
                    )
                )
            try:
                from clio_agent.gact.hooks import dispatch_semantic_event  # noqa: PLC0415

                dispatch_semantic_event(
                    project_hook(event, full=self.hooks_full),
                    session_id=event.session_id,
                )
            except Exception:  # noqa: BLE001,S110 - semantic hooks are observability side-effects; never crash the turn
                # SemanticEvent hooks are observability side-effects. They should
                # never mutate or crash the turn being observed.
                pass
        return full
