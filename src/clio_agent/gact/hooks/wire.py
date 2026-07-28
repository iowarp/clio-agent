"""The hook wire contract: envelope, tagged-union output, typed reasons (P2.2).

This module owns the *data* shapes the hook system speaks — the industry
exit-0/exit-2 subprocess wire (hooks-research §5.3) expressed as typed Python:

* :class:`HookEnvelope` — the JSON envelope written to a hook's stdin.
* :class:`HookDecision` — one hook's parsed tagged-union output.
* :class:`HookOutcome` — the merged decision across every hook on one event
  (``deny > ask > allow`` most-restrictive-wins; ``additionalContext``
  concatenated).
* :class:`HookInfraError` — a hook *infrastructure* failure (timeout / crash /
  missing binary / unparseable stdout). It is deliberately DISTINCT from a
  ``deny`` decision so the "hook failure != user rejection" invariant holds in
  shipped code.

It imports only stdlib + the tool-annotation classifier
(:mod:`clio_agent.tools.catalog`), never :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from clio_agent.tools.catalog import annotations_are_read_only

logger = logging.getLogger(__name__)

#: The wire schema version stamped on every envelope. Shipping this from v1 (which
#: no surveyed CLI did) lets the payload shape evolve without silently breaking
#: hook scripts written against an older contract (hooks-research §5.3).
SCHEMA_VERSION = 1

#: The tagged-union decisions this build understands. P2.3 promotes ``modify`` and
#: ``synthesize`` to first-class: they drive the already-wired ``tool_interceptor``
#: slot (``modify`` mutates the tool input; ``synthesize`` skips the real call and
#: fabricates the result). P2.6 promotes ``defer`` to first-class: the governance
#: outcome "suspend this yield point, accept the decision from an OUT-OF-BAND channel
#: (an API resolve / loop-inbox), resume when approved" (see
#: :mod:`clio_agent.gact.hooks.defer`). ``defer`` NEVER silently drops or auto-approves
#: — it persists a pending approval and parks the call/turn until resolved.
Decision = Literal["allow", "deny", "ask", "modify", "synthesize", "defer"]
_SUPPORTED_DECISIONS: frozenset[str] = frozenset(
    {"allow", "deny", "ask", "modify", "synthesize", "defer"}
)
#: No decision string is reserved-but-unimplemented any more (``defer`` shipped in
#: P2.6). Kept as an (empty) seam so a future reserved decision can be rejected with a
#: typed reason at :func:`parse_hook_output` without re-introducing the branch.
_RESERVED_DECISIONS: frozenset[str] = frozenset()

#: Most-restrictive-wins ordering (hooks-research invariant 3). A higher rank is
#: more restrictive and wins the merge. ``deny`` outranks ``defer`` so the tighten-only
#: invariant "deny still beats defer" holds: when one hook denies and another defers the
#: same event, the deny wins the merge (the call is blocked, never merely parked).
#: ``defer`` in turn outranks ``ask`` and the intercept decisions — a durable
#: out-of-band hold is a stronger governance posture than an interactive ask or a
#: synthesize/modify.
_DECISION_RANK: dict[str, int] = {
    "allow": 0,
    "modify": 1,
    "synthesize": 2,
    "ask": 3,
    "defer": 4,
    "deny": 5,
}


# --------------------------------------------------------------------------- #
# Typed reason catalog (mirrors the ``stream_fallback`` catalog): a structured,   #
# process-wide, queryable-after-the-fact record of every degraded hook path — an  #
# infra failure is NEVER silent (the no-silent-fallback ground rule).             #
# --------------------------------------------------------------------------- #
_HOOK_REASON_DEFINITIONS: dict[str, dict[str, Any]] = {
    "hook_timeout": {
        "severity": "warning",
        "detail": "hook exceeded its timeout and was killed",
    },
    "hook_missing_binary": {
        "severity": "error",
        "detail": "hook command binary was missing or not executable",
    },
    "hook_crashed": {
        "severity": "warning",
        "detail": "hook exited with a non-blocking (neither 0 nor 2) status",
    },
    "hook_unparseable_stdout": {
        "severity": "warning",
        "detail": "hook exited 0 but stdout was not parseable as the tagged-union output",
    },
    "hook_reserved_decision": {
        "severity": "warning",
        "detail": "hook returned a decision reserved for a later slice",
    },
    "hook_defer_no_session": {
        "severity": "warning",
        "detail": (
            "a PreToolUse hook returned 'defer' but there is no session to park the "
            "pending approval under; the call was denied fail-safe (never auto-approved)"
        ),
    },
    "hook_defer_timeout": {
        "severity": "warning",
        "detail": (
            "a deferred PreToolUse approval was not resolved within the configured "
            "bound; the parked call was released and DENIED fail-safe (never a silent "
            "auto-approve)"
        ),
    },
    "hook_defer_denied": {
        "severity": "warning",
        "detail": "a deferred approval was resolved as DENY out-of-band; a typed deny reached the model",
    },
    "hook_unknown_decision": {
        "severity": "warning",
        "detail": "hook returned an unrecognised decision value",
    },
    "hook_conflicting_intercept": {
        "severity": "warning",
        "detail": (
            "more than one winning-tier hook returned a modify/synthesize/rewrite "
            "payload for one event; the first by stable id order was applied"
        ),
    },
    "hook_fail_closed_deny": {
        "severity": "warning",
        "detail": "a deny-capable hook failed on infrastructure and was denied fail-closed",
    },
    "hook_untrusted_content_changed": {
        "severity": "warning",
        "detail": (
            "a loaded hook's content fingerprint changed since it was last trusted "
            "(e.g. a repo hook edited by a git pull); it is marked UNTRUSTED and will "
            "NOT run until re-approved — never a silent run of changed content"
        ),
    },
    "hook_modify_missing_input": {
        "severity": "warning",
        "detail": (
            "a modify decision carried no usable input Mapping (missing/malformed "
            "'input'); the tool ran with its ORIGINAL args instead"
        ),
    },
    "hook_synthesize_missing_llm_response": {
        "severity": "warning",
        "detail": (
            "a BeforeModel synthesize decision carried no 'llm_response'; the real "
            "LM was called instead of being skipped"
        ),
    },
    "hook_route_unresolved": {
        "severity": "warning",
        "detail": (
            "a BeforeModel model_override named an LM the route resolver could not "
            "resolve; the call ran on the DEFAULT model instead"
        ),
    },
    "hook_model_denied": {
        "severity": "warning",
        "detail": "a BeforeModel hook denied the model request; the call was blocked",
    },
    "stop_loop_cap": {
        "severity": "warning",
        "detail": (
            "a Stop hook kept blocking (re-driving) the turn; the bounded self-loop "
            "cap tripped, so the turn was settled DONE without another re-drive "
            "(never an infinite loop)"
        ),
    },
}

_HOOK_REASONS_MAX = 256
_HOOK_REASONS: "deque[dict[str, Any]]" = deque(maxlen=_HOOK_REASONS_MAX)
_HOOK_REASONS_LOCK = threading.Lock()


def hook_reasons() -> list[dict[str, Any]]:
    """Return a snapshot of recorded hook fallback reasons (bounded ring)."""

    with _HOOK_REASONS_LOCK:
        return list(_HOOK_REASONS)


def record_hook_reason(reason: str, **fields: Any) -> dict[str, Any]:
    """Record a structured hook fallback reason (no-silent-fallback).

    Appends to the bounded in-process ring and the ``stream_audit`` JSONL. Raises
    :class:`ValueError` for an unknown reason so a typo can never hide a real
    degradation behind a silent no-op.
    """

    definition = _HOOK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown hook fallback reason: {reason}")
    payload: dict[str, Any] = {"reason": reason, **definition, **fields}
    with _HOOK_REASONS_LOCK:
        _HOOK_REASONS.append(payload)
    logger.warning(
        "[clio-hooks] %s hook_id=%s event=%s",
        reason,
        fields.get("hook_id"),
        fields.get("event"),
    )
    try:
        from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

        stream_audit("hook.fallback", **payload)
    except Exception:  # noqa: BLE001,S110 - audit sink is best-effort; the ring is authoritative
        pass
    return payload


class HookInfraError(Exception):
    """A hook infrastructure failure — DISTINCT from a user/hook ``deny`` (invariant 2).

    Carries a typed ``reason`` (a key in :data:`_HOOK_REASON_DEFINITIONS`) so the
    dispatcher can apply the per-hook fail-closed posture and surface a message
    that never reads as "the user rejected this".
    """

    def __init__(self, reason: str, message: str, *, hook_id: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.hook_id = hook_id


def wire_annotations(raw: Any) -> dict[str, bool]:
    """Project MCP ``ToolAnnotations`` to the wire ``tool_annotations`` block.

    Fail-safe per the MCP spec (hooks-research §5.3 / M4): an absent or malformed
    annotation block is treated as the most-restrictive shape —
    ``destructive: true``, ``openWorld: true``, ``readOnly: false`` — so a policy
    written against capabilities never mistakes an unknown tool for a safe one.

    ``destructive`` reads the declared ``destructiveHint`` directly (the same
    pattern ``openWorld`` uses for ``openWorldHint``): it defaults ``True`` and
    only flips to ``False`` when a well-formed mapping carries a real boolean
    ``destructiveHint is False``. This lets a bounded, positively-declared
    non-destructive write (``readOnlyHint: false, destructiveHint: false,
    openWorldHint: false``) actually wire as ``destructive: false`` — a hook
    ``match: {annotations: {destructive: false}}`` can match it — instead of
    being derived (and inverted) from ``readOnly``, which previously reported
    EVERY non-read-only tool as destructive regardless of the annotation.
    """

    read_only = annotations_are_read_only(raw)
    open_world = True
    if isinstance(raw, Mapping) and raw.get("openWorldHint") is False:
        open_world = False
    destructive = True
    if isinstance(raw, Mapping) and raw.get("destructiveHint") is False:
        destructive = False
    return {"readOnly": read_only, "destructive": destructive, "openWorld": open_world}


@dataclass(frozen=True)
class HookEnvelope:
    """The context for one hook event — serialized to a hook's stdin as JSON.

    ``model_request`` is the P2.4 PUBLIC model-request contract (owner Q3): the
    minimal, versioned shape a ``BeforeModel``/``AfterModel`` hook sees — the target
    ``model`` id, the outgoing ``messages``, the sampling ``params``, and any
    declared ``tools``. It rides the same ``schema_version`` the whole envelope
    carries, so the shape can evolve without silently breaking a hook script written
    against an older contract.
    """

    hook_event_name: str
    session_id: str = ""
    turn_id: str = ""
    cwd: str = ""
    tool_name: str | None = None
    tool_input: Mapping[str, Any] | None = None
    tool_annotations: Mapping[str, Any] | None = None
    prompt: str | None = None
    payload: Mapping[str, Any] | None = None
    model_request: "ModelRequest | None" = None

    def to_json(self, *, hook_id: str) -> dict[str, Any]:
        """Build the JSON envelope written to the hook process stdin."""

        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "hook_id": hook_id,
            "hook_event_name": self.hook_event_name,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "cwd": self.cwd,
        }
        if self.tool_name is not None:
            body["tool_name"] = self.tool_name
        if self.tool_input is not None:
            body["tool_input"] = dict(self.tool_input)
        if self.tool_annotations is not None:
            body["tool_annotations"] = dict(self.tool_annotations)
        if self.prompt is not None:
            body["prompt"] = self.prompt
        if self.payload is not None:
            body["payload"] = dict(self.payload)
        if self.model_request is not None:
            body["model_request"] = self.model_request.to_json()
        return body


@dataclass(frozen=True)
class ModelRequest:
    """The P2.4 public, minimal, versioned shape of ONE outgoing model request.

    Deliberately small: the target ``model`` id, the chat ``messages``, the sampling
    ``params`` (temperature/max_tokens/… — api credentials are NEVER included), and
    any declared ``tools``. A ``BeforeModel`` hook reads it to decide
    synthesize/route/modify/deny; an ``AfterModel`` hook reads it alongside the
    produced ``response`` (carried on the envelope ``payload``).
    """

    model: str = ""
    messages: list[Any] = field(default_factory=list)
    params: Mapping[str, Any] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": list(self.messages),
            "params": dict(self.params),
            "tools": list(self.tools),
        }


@dataclass(frozen=True)
class HookDecision:
    """One hook's parsed tagged-union output.

    ``modify_input`` carries the mutated tool input for a ``modify`` decision;
    ``synthesize_result`` carries the fabricated result for a ``synthesize``
    decision; ``updated_output`` carries a PostToolUse observation rewrite
    (``updatedToolOutput``). ``synthesize_present`` disambiguates a genuine
    ``None`` synthesize result from "no result key".
    """

    decision: Decision = "allow"
    reason: str = ""
    additional_context: str = ""
    system_message: str = ""
    hook_id: str = ""
    modify_input: Mapping[str, Any] | None = None
    synthesize_result: Any = None
    synthesize_present: bool = False
    updated_output: str = ""
    # P2.4 model-hook payloads (BeforeModel/AfterModel). ``llm_response`` is the
    # canned/rewritten completion (a list[dict|str] like ``dspy.BaseLM`` returns, or
    # a bare string the wrapper wraps); ``llm_response_present`` disambiguates a
    # genuine ``None``/empty response from "no response key". ``model_override``
    # names an alternate LM for a route; ``request_patch`` rewrites the outgoing
    # ``messages``/``params`` for a redact.
    llm_response: Any = None
    llm_response_present: bool = False
    model_override: str = ""
    request_patch: Mapping[str, Any] | None = None


def parse_hook_output(
    raw: Mapping[str, Any] | None,
    *,
    hook_id: str,
    event: str,
) -> HookDecision:
    """Parse a hook's stdout JSON object into a :class:`HookDecision`.

    ``None`` / empty output is an ``allow`` (exit-0-empty = proceed). A reserved
    (``defer``) or unknown decision is recorded as a typed reason and treated as
    ``allow`` (non-blocking) — it never silently denies, and the reason is
    queryable. ``modify``/``synthesize`` are first-class (P2.3): their
    ``input``/``result`` payloads ride the decision to the ``tool_interceptor``.
    """

    if not raw:
        return HookDecision(decision="allow", hook_id=hook_id)
    decision = str(raw.get("decision") or "allow").lower()
    if decision in _RESERVED_DECISIONS:
        record_hook_reason("hook_reserved_decision", hook_id=hook_id, event=event, decision=decision)
        decision = "allow"
    elif decision not in _SUPPORTED_DECISIONS:
        record_hook_reason("hook_unknown_decision", hook_id=hook_id, event=event, decision=decision)
        decision = "allow"
    modify_input: Mapping[str, Any] | None = None
    if decision == "modify":
        candidate = raw.get("input")
        modify_input = candidate if isinstance(candidate, Mapping) else None
    synthesize_present = decision == "synthesize" and "result" in raw
    synthesize_result = raw.get("result") if decision == "synthesize" else None
    # P2.4 model payloads — parsed for EVERY decision so an AfterModel rewrite
    # (which may ride an ``allow``) and a BeforeModel synthesize/route/modify are all
    # picked up. ``llm_response_present`` records the key's presence explicitly.
    llm_response_present = "llm_response" in raw
    llm_response = raw.get("llm_response") if llm_response_present else None
    model_override = str(raw.get("model_override") or "")
    patch_candidate = raw.get("request_patch")
    request_patch = patch_candidate if isinstance(patch_candidate, Mapping) else None
    return HookDecision(
        decision=decision,  # type: ignore[arg-type]
        reason=str(raw.get("reason") or ""),
        additional_context=str(raw.get("additionalContext") or ""),
        system_message=str(raw.get("systemMessage") or ""),
        hook_id=hook_id,
        modify_input=modify_input,
        synthesize_result=synthesize_result,
        synthesize_present=synthesize_present,
        updated_output=str(raw.get("updatedToolOutput") or ""),
        llm_response=llm_response,
        llm_response_present=llm_response_present,
        model_override=model_override,
        request_patch=request_patch,
    )


@dataclass
class HookOutcome:
    """The merged decision across every hook that ran for one event."""

    decision: Decision = "allow"
    reason: str = ""
    additional_context: str = ""
    system_message: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    modify_input: Mapping[str, Any] | None = None
    synthesize_result: Any = None
    synthesize_present: bool = False
    updated_output: str = ""
    # P2.4 model-hook merged payloads.
    llm_response: Any = None
    llm_response_present: bool = False
    model_override: str = ""
    request_patch: Mapping[str, Any] | None = None

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def is_defer(self) -> bool:
        """Whether the merged decision is a durable OUT-OF-BAND defer (P2.6).

        The consuming yield points (the PreToolUse gate, the turn-ending seams) park
        the operation and persist a pending approval rather than allow/deny inline.
        ``deny`` outranks ``defer`` in the merge, so this is never true when any hook
        denied the same event (deny beats defer).
        """

        return self.decision == "defer"

    @property
    def is_modify(self) -> bool:
        return self.decision == "modify" and self.modify_input is not None

    @property
    def is_synthesize(self) -> bool:
        return self.decision == "synthesize"

    @property
    def is_model_synthesize(self) -> bool:
        """Whether a BeforeModel hook wants to SKIP the real LM with a canned response."""

        return self.decision == "synthesize" and self.llm_response_present

    @property
    def has_request_patch(self) -> bool:
        return self.decision == "modify" and self.request_patch is not None

    @classmethod
    def merge(cls, decisions: list[HookDecision], records: list[dict[str, Any]]) -> "HookOutcome":
        """Merge per-hook decisions most-restrictive-wins (invariant 3).

        ``deny > defer > ask > synthesize > modify > allow``; the winning tier's
        reasons are joined; every hook's ``additionalContext``/``systemMessage`` is
        concatenated in order. ``deny`` outranking ``defer`` is the tighten-only
        "deny beats defer" invariant (P2.6). When the winning tier is an INTERCEPT
        (``modify``/``synthesize``) or any hook carries a PostToolUse
        ``updatedToolOutput`` rewrite, the FIRST such payload by stable id order is
        applied and a ``hook_conflicting_intercept`` reason is recorded if more
        than one competes — never a silent last-writer-wins.
        """

        outcome = cls(records=records)
        if not decisions:
            return outcome
        winner_rank = max(_DECISION_RANK.get(d.decision, 0) for d in decisions)
        winning = [d for d in decisions if _DECISION_RANK.get(d.decision, 0) == winner_rank]
        outcome.decision = winning[0].decision
        outcome.reason = "\n".join(d.reason for d in winning if d.reason)
        outcome.additional_context = "\n".join(
            d.additional_context for d in decisions if d.additional_context
        )
        outcome.system_message = "\n".join(
            d.system_message for d in decisions if d.system_message
        )
        if outcome.decision == "modify":
            movers = [d for d in winning if d.modify_input is not None]
            if len(movers) > 1:
                # D5 / invariant 3: two hooks both returning a tool-input ``modify``
                # is an ERROR, not first/last-writer-wins — applying an arbitrary one
                # (even deterministically by id) would silently pick a winner. Block
                # the call instead and surface a diagnostic naming the conflicters,
                # and record the typed conflict reason (no-silent-fallback).
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=movers[0].hook_id,
                    decision="modify",
                )
                outcome.decision = "deny"
                outcome.reason = (
                    "Conflicting 'modify' decisions from multiple PreToolUse hooks ("
                    + ", ".join(d.hook_id for d in movers)
                    + "); the tool was blocked rather than applying an arbitrary one."
                )
                outcome.modify_input = None
            elif len(movers) == 1:
                outcome.modify_input = movers[0].modify_input
            elif not any(d.request_patch is not None or d.model_override for d in winning):
                # A ``modify`` carrying NO usable payload at all (no ``input`` Mapping,
                # and — for a model modify — no request_patch/model_override): the
                # intercept intent would otherwise vanish silently (the boundary falls
                # through to "nothing to intercept" and runs unmodified). Record it as a
                # typed, queryable degradation (no-silent-fallback).
                record_hook_reason(
                    "hook_modify_missing_input",
                    hook_id=winning[0].hook_id,
                    decision="modify",
                )
        elif outcome.decision == "synthesize":
            outcome.synthesize_result = winning[0].synthesize_result
            outcome.synthesize_present = winning[0].synthesize_present
            if len(winning) > 1:
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=winning[0].hook_id,
                    decision="synthesize",
                )
        rewrites = [d for d in decisions if d.updated_output]
        if rewrites:
            outcome.updated_output = rewrites[0].updated_output
            if len(rewrites) > 1:
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=rewrites[0].hook_id,
                    decision="updatedToolOutput",
                )
        cls._merge_model_payloads(outcome, winning)
        return outcome

    @staticmethod
    def _merge_model_payloads(outcome: "HookOutcome", winning: list[HookDecision]) -> None:
        """Fold the P2.4 model payloads (llm_response / model_override / request_patch),
        scoped to the WINNING decision tier ONLY — consistent with how ``modify_input``/
        ``synthesize_result`` above are scoped to ``winning``.

        The consuming gates key off the WINNING decision, not off "any hook said so":
        ``is_model_synthesize`` requires ``decision == "synthesize"``, ``has_request_patch``
        (and the hooked_lm route branch) require ``decision == "modify"``. Gathering a
        payload from ALL decisions — irrespective of tier — let a non-winning hook's
        ``llm_response``/``model_override``/``request_patch`` be picked up in place of the
        winning hook's own payload (e.g. an ``allow``/``modify`` hook sorted earlier by id
        also carrying an ``llm_response``, while a DIFFERENT hook's ``synthesize`` actually
        wins the merge) — the wrong value would then enter context/routing even though the
        gate correctly identified which decision won. Scoping to ``winning`` closes that:
        a payload only counts when it rides the SAME decision that won the merge.
        ``winning`` is exactly the decisions sharing the outcome's decision string (the
        ``_DECISION_RANK`` mapping is injective, so equal rank implies equal decision) —
        an AfterModel rewrite rides on whatever tier wins that dispatch (ordinarily
        ``allow``, since AfterModel hooks have no reason to return anything else) and is
        scoped the same way for consistency.

        Within the winning tier, first-by-stable-id wins and a second producer records
        the typed ``hook_conflicting_intercept`` reason — never a silent last-writer-wins.
        """

        responders = [d for d in winning if d.llm_response_present]
        if responders:
            outcome.llm_response = responders[0].llm_response
            outcome.llm_response_present = True
            if len(responders) > 1:
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=responders[0].hook_id,
                    decision="llm_response",
                )
        routers = [d for d in winning if d.model_override]
        if routers:
            outcome.model_override = routers[0].model_override
            if len(routers) > 1:
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=routers[0].hook_id,
                    decision="model_override",
                )
        patchers = [d for d in winning if d.request_patch is not None]
        if patchers:
            outcome.request_patch = patchers[0].request_patch
            if len(patchers) > 1:
                record_hook_reason(
                    "hook_conflicting_intercept",
                    hook_id=patchers[0].hook_id,
                    decision="request_patch",
                )


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Banner-tolerant parse of hook stdout (hooks-research C8).

    A shell-profile banner printed before the JSON is common and the #1 hook
    support issue. Scan for the first ``{`` that begins a decodable JSON object
    (via ``raw_decode``, which tolerates trailing content/whitespace) and return
    it. Returns ``None`` when the text carries no JSON object at all — the caller
    decides whether that is "empty => allow" or a diagnosable unparseable error.
    """

    if not text or not text.strip():
        return None
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        idx = text.find("{", idx + 1)
    return None
