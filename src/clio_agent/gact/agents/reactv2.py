"""clio ReActV2 subclass — the production expert loop (#901; sole loop since v0.8.0).

This is clio's subclass of dspy's ``ReActV2`` plus the two seams that make its
append-only ``dspy.History`` composition ride clio's ARC live plane and frozen
wire contract. The classic ``_RetainingReAct`` and its ``CLIO_REACTV2`` switch
were deleted in the v0.8.0 cleanup; :mod:`clio_agent.gact.agents.runtime`
selects this class unconditionally (design ``901_reactv2_design.md``).

**Why V2 at all** (design §1–3): ReActV2 composes each turn as an append-only
``dspy.History`` of structured messages instead of one ever-growing re-rendered
``trajectory`` string, so a provider's prompt-cache sees a byte-stable prefix across
``self.react`` calls (the #891 payoff). clio's own ARC compact/delete op becomes the
*sole* prefix-reset author (V2 has no ``truncate_trajectory``).

**S1 — the frozen-contract signature defense** (§4, risk 1): clio's ReAct-internal
``next_thought`` field is typed plain ``str``, NOT ``dspy.Reasoning``.
``dspy.Reasoning.adapt_to_native_lm_feature`` deletes the Reasoning field from the
signature and sets ``reasoning_effort`` on any reasoning-capable model, which would
route ``next_thought`` onto the provider's *native reasoning channel* (clio's
thinking lane) instead of the visible ``[[ ## next_thought ## ]]`` response lane —
inverting the frozen wire contract (thinking = provider CoT ONLY; next_thought =
response). Typing it ``str`` keeps it a text-rendered field and leaves the #877
marker-split path unchanged.

**S2 — the ARC fold seam** (§5, design B): :func:`segments_to_messages` folds the
MATERIALIZED ARC live plane (the ordered thought/tool_call/observation segments the
loop wrote and the ARC ops mutate) into the ``dspy.History`` message list ReActV2
consumes. The read seam is a custom adapter override of ``format_conversation_history``
(:func:`override_history_inputs_from_arc`, wired from
:class:`clio_agent.lm.adapters.LenientChatAdapter`): the analog of the classic
``_format_trajectory`` override. It sources straight from ARC's materialized render,
so ARC stays the single wire source and an out-of-band ARC edit (delete/summarize/
insert/append) changes the next prompt. **Binding owner condition:** the fold reads
the materialized plane (``ARCMemory.render_segments`` → ``SegmentStore.render``),
NEVER re-derives context from the canonical semantic-event log per request; ARC ops
(compact/delete) remain the sole reset authors of the History prefix.

**S3 — the #878 contract rework onto submit** (§4, risk 2): ReActV2 has no ``extract``
step — the final outputs ride the internal ``submit`` tool's typed args and flow to
the returned ``Prediction`` unchanged. The classic #878 extract-suppression (the tap
gate ``react_extract_field_suppressed``) has nothing to fire on, so this class
re-expresses its intent on the submit turn: :meth:`_RetainingReActV2._execute_tool_calls`
records a typed ``react_submit_field_suppressed`` reason for every submit output field
(its VALUE flows to the return contract, it is NOT emitted as visible text — in V2 the
submit args ride the ``tool_calls`` field, never a visible ``answer``/``reasoning``
lane) and a ``react_submit_invalid_output`` reason when the ``submit`` tool rejects a
typed/missing output arg (a degraded path — the value did NOT flow). Both are recorded
through the stream-audit sink in the ``stream_fallback`` house style so no routing is
silent. The classic path's #878 handling is byte-untouched (it lives in
``lm_activity``/``streaming`` and is never reached from V2).

**Import placement**: ``dspy`` is imported at module scope on purpose. This module is
itself *deferred* — nothing imports it at package load; only
:func:`retaining_reactv2_cls` (reached from ``runtime._retaining_react_cls`` when the
kill-switch is ON) or the ``LenientChatAdapter`` read seam or a test pulls it in.
Every ARC / gact-context / audit import is lazy inside the functions that need it, so
the class stays a real, importable, testable module-scope unit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dspy
from dspy.adapters.types.tool import Tool, ToolCallResults, ToolCalls
from dspy.predict.react_v2 import _json_schema_for_annotation

from clio_agent.gact.agents.reactv2_submit import (
    REACT_FORCED_SUBMIT_REJECTED as REACT_FORCED_SUBMIT_REJECTED,
)
from clio_agent.gact.agents.reactv2_submit import (
    active_react_scope_safe as _active_react_scope_safe,
)
from clio_agent.gact.agents.reactv2_submit import forced_submit
from clio_agent.gact.agents.reactv2_submit import record_submit_audit as _record_submit_audit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.arc.schema import Segment

# ---- typed reason catalog (no-silent-fallback, stream_fallback house style) ----
#
# Each is a queryable ``duplicate_reason`` recorded on the ``stream_audit`` sink so a
# routing / degradation decision on the V2 path is never silent (see the module
# docstring, S3, and the #772 no-silent-fallback rule).

# A submit output field's VALUE flowed to the return contract (the final Prediction /
# the delegation ``output`` behind *show more*) and was NOT emitted as visible text.
REACT_SUBMIT_FIELD_SUPPRESSED = "react_submit_field_suppressed"
# The ``submit`` tool rejected a typed/missing final-output arg — a degraded turn: the
# value did NOT flow to the return contract (the loop force-submits / ends instead).
REACT_SUBMIT_INVALID_OUTPUT = "react_submit_invalid_output"
# The ARC materialized-plane read raised while folding the History prefix; the seam
# fell back to ReActV2's own internal append-only history for this call.
REACTV2_ARC_HISTORY_READ_FAILED = "reactv2_arc_history_read_failed"
# S4 — the bounded submit-repair re-ask: the loop ended without every declared output
# field, so the parent was RE-ASKED (a forced submit carrying a schema-derived hint).
# Recorded per attempt so the re-ask is queryable; the model — not clio — produces the
# outputs (no deterministic fabrication).
REACT_SUBMIT_REPAIR_ATTEMPTED = "react_submit_repair_attempted"
# The bounded submit-repair budget was spent and the declared outputs are STILL missing
# — a degraded turn (the value did NOT flow). Never a fabricated value: the missing
# fields stay absent (a declared Pydantic default is honored by ``_make_submit_tool``,
# which is author intent, not fabrication).
REACT_SUBMIT_REPAIR_EXHAUSTED = "react_submit_repair_exhausted"


class _RetainingReActV2(dspy.ReActV2):  # type: ignore[misc, name-defined]
    """clio subclass of dspy's experimental ``ReActV2`` (see module docstring)."""

    def _make_react_signature(self) -> type[dspy.Signature]:
        """Build the ReAct-internal predict signature, retyping ``next_thought``.

        Mirrors ``dspy.predict.react_v2.ReActV2._make_react_signature`` and then
        applies the single frozen-contract change: ``next_thought`` becomes plain
        ``str`` instead of ``dspy.Reasoning`` (see the class/module docstring). Using
        the public :meth:`dspy.Signature.with_updated_fields` — rather than
        re-authoring the whole method body — keeps clio in lockstep with upstream:
        only the field *type* is overridden, every other field, instruction, and
        ordering flows through unchanged. A unit test guards the resulting field
        types / signature shape.
        """
        signature = super()._make_react_signature()
        return signature.with_updated_fields("next_thought", type_=str)

    def _make_submit_tool(self) -> Tool:
        """Build the internal ``submit`` tool, honoring declared Pydantic defaults (#901 S4).

        Stock ``ReActV2._make_submit_tool`` requires EVERY declared output field: an
        omitted arg raises ``ValueError`` and the turn is a recorded rejection (the S3
        pinned limitation). This override resolves it the sanctioned dspy way — a field
        the *pack author declared with a Pydantic default* (``dspy.OutputField(default=…)``
        / ``default_factory=…``) is **droppable-with-that-default**: when the model omits
        it, ``submit`` fills the declared default and the turn succeeds. A field WITHOUT a
        declared default stays required (an omission is still the recorded
        ``react_submit_invalid_output`` rejection). This is *format-only* correction
        honoring a declared default — NOT clio fabricating a value the author never
        specified (superseding principle #2 / #5). Field ``args``/``arg_types`` are
        identical to stock so the wire schema is unchanged.
        """
        output_fields = self.signature.output_fields
        output_names = list(output_fields)
        droppable = {
            name: default
            for name, field in output_fields.items()
            for has_default, default in [_field_declared_default(field)]
            if has_default
        }

        def submit(**kwargs: Any) -> dict[str, Any]:
            missing = [n for n in output_names if n not in kwargs and n not in droppable]
            if missing:
                raise ValueError(f"Missing required final output field(s): {', '.join(missing)}")
            return {n: (kwargs[n] if n in kwargs else droppable[n]) for n in output_names}

        args = {
            name: _json_schema_for_annotation(field.annotation)
            for name, field in output_fields.items()
        }
        arg_types = {name: field.annotation for name, field in output_fields.items()}
        return Tool(
            submit,
            name="submit",
            desc="Submit the final outputs for the task.",
            args=args,
            arg_types=arg_types,
        )

    def _forced_submit(
        self,
        history: dspy.History,
        pending_inputs: dict[str, Any],
        break_reason: str,
        turn_index: int,
    ) -> Any:
        """Finalize through the sole legal ``submit`` operation.

        The extracted owner keeps provider tool-choice differences from inviting an
        unrelated operation and audits every rejected finalization.
        """
        return forced_submit(self, history, pending_inputs, break_reason, turn_index)

    def forward(self, **input_args: Any) -> Any:
        """Run the V2 loop, retain the History, and bounded-repair a missing submit (#901 S4/S6).

        Wraps the *instrumented* V2 loop
        (:func:`clio_agent.gact.agents.reactv2_events.instrumented_forward` — the
        append-only ``ReActV2.forward`` mirror that drives clio's ARC live-plane writes,
        the semantic-event highway, and the proactive auto-compaction trigger) with the
        two S4 hooks that re-express the classic retention/repair path (design §7):

        1. **Retention** — install a fresh trajectory cell and publish the retained
           ``History`` + pending inputs (the V2 analog of the classic
           ``publish_trajectory({trajectory, input_args})`` before ``extract``), so the
           trace/failure-capture consumers and the repair entry can read exactly what the
           loop produced.
        2. **Bounded repair** — when the loop ends WITHOUT every declared output field
           (a forced/failed submit), RE-ASK the parent: a forced submit carrying a
           schema-derived hint, up to a bounded budget, and let the model decide. clio
           never fabricates the outputs (see :meth:`_bounded_submit_repair`).
        """
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.agents.reactv2_events import instrumented_forward  # noqa: PLC0415
        from clio_agent.providers.claude_code_stateful import stateful_scope  # noqa: PLC0415

        _ctx.install_trajectory_cell()
        pending = {
            name: input_args[name] for name in self.signature.input_fields if name in input_args
        }
        # Bind a fresh per-forward stateful scope token (#901 stateful-delta): every
        # ``self.react`` LM call inside this loop shares ONE claude_code SDK session so
        # consecutive append-only prompts ride a byte-stable prefix and send only their
        # delta tail. A fresh token per forward means a new turn always restarts the
        # session, and parallel experts never share one; the scope releases its session
        # registry entries on exit (the #900 explicit-teardown seam). Inert unless the
        # stateful_delta flag is ON and the provider is claude_code.
        with stateful_scope():
            pred = instrumented_forward(self, **input_args)
            self._publish_retained_history(pred, pending)
            return self._bounded_submit_repair(pred, pending)

    def _maybe_autocompact(self) -> None:
        """Proactive, scope-aware auto-compaction — the V2 trigger (#901 S6).

        When ``prompt_tokens / context_window`` crosses the threshold, fold the live
        working set into ONE ARC ``summarize`` op (V2's sole prefix-reset author; LM
        helpers resolve via ``runtime`` for test-monkeypatch). No-op on ARC-off/no-summary.
        """
        from clio_agent.gact import context as _ctx  # noqa: PLC0415
        from clio_agent.gact.agents import runtime as _rt  # noqa: PLC0415
        from clio_agent.gact.agents.reactv2_events import _arc_scope  # noqa: PLC0415
        from clio_agent.gact.runtime.context_tokens import (  # noqa: PLC0415
            _session_autocompact_preferences,
        )
        from clio_agent.providers.stateful_common import (
            note_prefix_reset_for_active_scope,  # noqa: PLC0415, E501
        )

        arc, session, scope = _arc_scope()
        if arc is None:
            return
        app = _ctx.active_app()
        sessions = getattr(getattr(app, "state", None), "sessions", None)
        session_row = sessions.get(session) if sessions is not None else None
        enabled, threshold = _session_autocompact_preferences(
            getattr(session_row, "metadata", None)
        )
        if not enabled:
            return
        window = _ctx.active_react_context_window()
        last = _rt._last_prompt_tokens()
        if not window or not last:
            return
        if (last / window) < threshold:
            return
        live = arc.render_working_set(session, scope)
        if len(live) <= 1:
            return
        summary = _rt._summarize_segments_llm(live)
        if not summary:
            return
        arc.summarize_segments(session, scope, [s.id for s in live], {"text": summary})
        # The summarize op rewrote the History prefix: flag the active stateful scope so
        # both legs' next send is a typed ``ops_reset`` (not a generic ``prefix_mismatch``);
        # inert when the feature is off / no scope active (#891).
        note_prefix_reset_for_active_scope("ops_reset")

    def _publish_retained_history(self, pred: Any, pending: dict[str, Any]) -> None:
        """Publish the retained ``History`` + pending inputs to the active trajectory cell.

        The V2 analog of the classic ``_ctx.publish_trajectory({trajectory, input_args})``:
        retains the append-only ``history.messages`` (what the loop actually produced) and
        the signature inputs so :func:`reforce_submit_over_retained_history` (the repair
        entry) and any failure-capture consumer can read them. No-op when no cell is
        installed (mirrors the classic publish contract).
        """
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        history = getattr(pred, "history", None)
        messages = list(history.messages) if history is not None else []
        _ctx.publish_trajectory(
            {
                "history": messages,
                "input_args": dict(pending),
                "termination_reason": str(getattr(pred, "termination_reason", "") or ""),
            }
        )

    def _bounded_submit_repair(self, pred: Any, pending: dict[str, Any]) -> Any:
        """Bounded RE-ASK when the loop ended without every declared output field (#901 S4).

        The V2 analog of the deleted classic re-extract repair (v0.8.0). When
        every declared output is already present (the normal submit), this is a zero-cost
        pass-through. Otherwise it re-asks the parent up to :func:`_submit_repair_attempts`
        times — each a forced submit over the retained History carrying a schema-derived
        hint (:meth:`_submit_repair_hint`) that names the missing fields. **The model
        decides**: the re-ask re-drives the react predict and the model re-emits ``submit``;
        clio never fabricates the values. The loop is ALWAYS bounded (``range(bound)`` — the
        sabotage tripwire). Every attempt records ``react_submit_repair_attempted``; a spent
        budget with fields still missing records ``react_submit_repair_exhausted`` (a
        recorded degraded turn — the missing fields stay absent, never fabricated).
        """
        if self._declared_outputs_present(pred):
            return pred
        agent_id = _active_react_scope_safe()
        bound = _submit_repair_attempts()
        for _attempt in range(bound):
            hint = self._submit_repair_hint(pred)
            _record_submit_audit(
                REACT_SUBMIT_REPAIR_ATTEMPTED,
                agent_id=agent_id,
                field="submit",
                text=hint,
                suppressed=False,
            )
            repaired = reforce_submit_over_retained_history(self, hint)
            if repaired is None:
                break
            if self._declared_outputs_present(repaired):
                return repaired
            pred = repaired
        if not self._declared_outputs_present(pred):
            _record_submit_audit(
                REACT_SUBMIT_REPAIR_EXHAUSTED,
                agent_id=agent_id,
                field="submit",
                text=", ".join(self._missing_declared_outputs(pred)),
                suppressed=False,
            )
        return pred

    def _missing_declared_outputs(self, pred: Any) -> list[str]:
        """Declared user-signature output fields absent from ``pred`` (structured, no prose).

        Reads the SCHEMA (``signature.output_fields``) against the prediction's produced
        keys — a structured decision, never a keyword/prose heuristic on model text
        (superseding principle #1).
        """
        present = set(pred.keys()) if hasattr(pred, "keys") else set()
        return [name for name in self.signature.output_fields if name not in present]

    def _declared_outputs_present(self, pred: Any) -> bool:
        """Whether every declared user-signature output field is present on ``pred``."""
        return not self._missing_declared_outputs(pred)

    def _submit_repair_hint(self, pred: Any) -> str:
        """Build the schema-derived RE-ASK hint naming the missing declared outputs (#901 S4).

        Derived purely from the signature's declared outputs vs. the produced keys — a
        structured instruction, NOT prose-keyword matching on the model's text. Fed back
        via the ``question`` input on the forced-submit re-ask so the model can self-correct
        (the clio "re-ask when something is missing" bounded repair; the model fills the
        fields, clio does not).
        """
        missing = ", ".join(f"`{name}`" for name in self._missing_declared_outputs(pred))
        declared = ", ".join(f"`{name}`" for name in self.signature.output_fields)
        return (
            "SUBMIT-REPAIR (your previous response ended WITHOUT a valid `submit` for "
            f"required output field(s): {missing}). Call the `submit` tool now, providing "
            f"EVERY declared output field ({declared}) with a correct, non-empty value "
            "consistent with the evidence you already gathered. Do NOT add fields outside "
            "the declared outputs, and do NOT drop any declared field."
        )

    def _execute_tool_calls(
        self, tool_calls: ToolCalls
    ) -> tuple[ToolCallResults, dict[str, Any] | None]:
        """Execute the step's tool calls, then audit the ``submit`` turn (#901 S3).

        Delegates verbatim to ``ReActV2._execute_tool_calls`` (the values / final
        outputs are produced exactly as upstream), then records the typed submit-turn
        reasons that re-express #878's intent on V2's submit path. This is the single
        chokepoint the ``submit`` tool runs through for BOTH the normal loop
        (``forward``) and the forced tail (``_forced_submit``), so the audit fires
        wherever a final output is produced — no forward mirror is needed.
        """
        results, final_outputs = super()._execute_tool_calls(tool_calls)
        self._audit_submit_tool_calls(tool_calls, results, final_outputs)
        return results, final_outputs

    def _audit_submit_tool_calls(
        self,
        tool_calls: ToolCalls,
        results: ToolCallResults,
        final_outputs: dict[str, Any] | None,
    ) -> None:
        """Record the typed submit-turn reasons (#901 S3), never silently.

        For the ``submit`` call in this step:

        * on success (``final_outputs`` produced), record one
          ``react_submit_field_suppressed`` per final-output field — its VALUE rides
          the returned ``Prediction`` (the parent's return contract, rendered behind
          *show more*) and is NOT emitted as a visible text lane (V2 submit args ride
          the ``tool_calls`` field, so there is no ``answer``/``reasoning`` visible
          copy to suppress — the reason is the record that the value was routed);
        * on an error result (the ``submit`` tool rejected a typed/missing output
          arg), record ``react_submit_invalid_output`` with the rejection text — a
          degraded turn where the value did NOT flow.

        Best-effort: auditing must never break the loop. The stream-audit sink is a
        no-op unless configured, so this is free on the hot path.
        """
        result_by_id = {r.call_id: r for r in results.tool_call_results if r.call_id is not None}
        agent_id = _active_react_scope_safe()
        for call in tool_calls.tool_calls:
            if call.name != "submit":
                continue
            result = result_by_id.get(call.id)
            if result is not None and result.is_error:
                _record_submit_audit(
                    REACT_SUBMIT_INVALID_OUTPUT,
                    agent_id=agent_id,
                    field="submit",
                    text=str(result.value),
                    suppressed=False,
                )
                continue
            if final_outputs is None:
                continue
            for field, value in final_outputs.items():
                _record_submit_audit(
                    REACT_SUBMIT_FIELD_SUPPRESSED,
                    agent_id=agent_id,
                    field=field,
                    text=_stringify_value(value),
                    suppressed=True,
                )


def retaining_reactv2_cls() -> type[Any]:
    """Return the clio ReActV2 subclass — the V2 leg of ``_retaining_react_cls``.

    Parallels :func:`clio_agent.gact.agents.runtime._retaining_react_cls`'s classic
    factory so the kill-switch is a single branch. The class is a module-level
    constant (no per-base cache is needed: unlike the classic path, clio binds
    directly to the concrete ``dspy.ReActV2`` here rather than a test-monkeypatched
    ``dspy.ReAct``).
    """
    return _RetainingReActV2


# ----------------------------------------------------------------------------- #
# S4 — retention + bounded submit-repair (the V2 analog of the classic re-extract) #
# ----------------------------------------------------------------------------- #


def reforce_submit_over_retained_history(program: Any, hint: str) -> Any:
    """Re-drive ONE forced ``submit`` over the RETAINED History, steered by ``hint`` (#901 S4).

    The submit-repair entry, wired from the builders repair ladder since v0.8.0
    (the classic extract-only re-run died with the classic loop): V2 has no
    ``extract``, so the repair re-drives a forced ``submit`` over the retained
    ``History`` (design §7).
    Reads the retained ``{"history", "input_args"}`` published by
    :meth:`_RetainingReActV2._publish_retained_history` (via ``_ctx.active_trajectory()`` —
    the exact cell the classic re-extract reads), appends the schema-derived repair ``hint``
    to the ``question`` input so the model can self-correct, and calls the stock
    ``_forced_submit`` (``tool_choice: submit``). The tool loop is NOT restarted — the
    retained History is reused, only the final typed output is re-emitted.

    **The model decides**: the forced submit re-asks the react predict and the model
    re-emits ``submit`` with the outputs; clio fabricates nothing. Returns the resulting
    ``Prediction`` (which may still lack outputs if the model omits them again — the caller
    bounds the retries), or ``None`` when there is no retained History (the caller then
    stops — no unbounded loop).

    Args:
        program: The active :class:`_RetainingReActV2` instance.
        hint: The schema-derived repair instruction naming the missing declared outputs.

    Returns:
        A ``dspy.Prediction`` from the re-driven forced submit, or ``None``.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    retained = _ctx.active_trajectory()
    messages = retained.get("history") if isinstance(retained, dict) else None
    if not messages:
        return None
    input_args = dict((retained or {}).get("input_args") or {})
    if input_args.get("question"):
        input_args["question"] = f"{input_args['question']}\n\n{hint}"
    # Re-drive over a FRESH History copy of the retained messages so each bounded re-ask is
    # an independent sample from the same retained state (mirrors the classic re-extract,
    # which re-runs over the same retained trajectory each attempt). ``_forced_submit``'s
    # internal handling (AdapterParseError / ValueError / ContextWindowExceededError) means
    # a genuine bug is the only thing that surfaces here — no blind swallow needed.
    history = dspy.History(messages=list(messages))
    return program._forced_submit(history, input_args, "submit_repair", len(messages))


def _submit_repair_attempts() -> int:
    """Bounded budget of forced-submit re-asks after a missing-output loop end (#901 S4).

    Mirrors ``builders._extract_repair_attempts`` for the V2 path. Default 3; override
    ``CLIO_SUBMIT_REPAIR_ATTEMPTS`` / ``limits.submit_repair_attempts``. Always clamped to
    ``>= 0`` so :meth:`_RetainingReActV2._bounded_submit_repair` can never loop unbounded
    (the S4 sabotage tripwire).
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        n = int(
            conf.resolve(
                "limits.submit_repair_attempts",
                env="CLIO_SUBMIT_REPAIR_ATTEMPTS",
                default=3.0,
                cast=conf.as_float,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break a turn; mirror the classic default
        return 3
    return max(0, n)


def _field_declared_default(field: Any) -> tuple[bool, Any]:
    """Return ``(has_default, value)`` for a dspy output ``FieldInfo`` (#901 S4).

    A dspy ``OutputField`` is a ``pydantic.fields.FieldInfo`` (superseding principle #5):
    a declared default surfaces as a concrete ``field.default`` (not
    ``PydanticUndefined``) or a ``field.default_factory``. Returns ``(True, <default>)``
    for a field the author declared droppable-with-a-value, else ``(False, None)`` for a
    genuinely required field.
    """
    from pydantic_core import PydanticUndefined  # noqa: PLC0415

    default = getattr(field, "default", PydanticUndefined)
    if default is not PydanticUndefined:
        return True, default
    factory = getattr(field, "default_factory", None)
    if factory is not None:
        return True, factory()
    return False, None


# ----------------------------------------------------------------------------- #
# S2 — the ARC fold seam: materialized live plane -> dspy.History messages       #
# ----------------------------------------------------------------------------- #


def segments_to_messages(segments: list[Segment]) -> list[dict[str, Any]]:
    """Fold ordered LIVE ARC segments into ReActV2's ``dspy.History`` message list.

    The V2 analog of :func:`clio_agent.arc.segments.segments_to_keys`. It reuses that
    exact trajectory-dict projection (imported, not re-implemented, so the turn
    grouping / gapless re-indexing is byte-identical to the classic path and
    segments.py is not regrown against its size ratchet), then maps each recomputed
    turn ``i`` into one ``ReActV2`` history event:

    * ``thought_i``          -> ``event["next_thought"] = <text>`` (str, the response
      lane — never ``dspy.Reasoning``, matching the S1 signature defense);
    * ``tool_name_i`` +
      ``tool_args_i``        -> ``event["tool_calls"] = ToolCalls([ToolCall(id=
      "call_i_0", name, args)])`` with, when ``observation_i`` is present, the
      observation merged in as the tool call's ``ToolCallResults`` (``id`` scheme
      matches ``ReActV2._ensure_tool_call_ids`` so the folded events line up with a
      stock V2 loop);
    * a lone ``observation_i`` (e.g. a compaction ``summary`` segment, which
      ``segments_to_keys`` renders as an ``observation`` with no tool call) ->
      ``event["next_thought"] = <text>`` so its content still reaches the wire.

    Pure and side-effect-free. Robust to malformed segment content: a missing text is
    ``""`` and a non-dict ``args`` is coerced to ``{}`` so a bad write can never raise
    here. This fold produces only the per-turn events; the static task inputs
    (``question`` + ``tools``) are folded into the HEAD event LATER, at the adapter read
    seam (:func:`override_history_inputs_from_arc`), matching stock ReActV2 so consecutive
    wire renders are strict prefix extensions — the #901 append-only invariant.

    Args:
        segments: Ordered LIVE segments (``ARCMemory.render_segments`` output).

    Returns:
        A list of history-event dicts keyed by react-signature field names.
    """
    from clio_agent.arc.segments import segments_to_keys  # noqa: PLC0415

    keys = segments_to_keys(segments)
    turn_count = _turn_count(keys)
    messages: list[dict[str, Any]] = []
    for i in range(turn_count):
        event: dict[str, Any] = {}
        if f"thought_{i}" in keys:
            event["next_thought"] = _as_text(keys[f"thought_{i}"])
        if f"tool_name_{i}" in keys:
            args = keys.get(f"tool_args_{i}")
            call = ToolCalls.ToolCall(
                id=f"call_{i}_0",
                name=_as_text(keys[f"tool_name_{i}"]),
                args=args if isinstance(args, dict) else {},
            )
            tool_calls = ToolCalls(tool_calls=[call])
            if f"observation_{i}" in keys:
                tool_calls = tool_calls.model_copy(
                    update={
                        "tool_call_results": ToolCallResults.from_tool_calls_and_values(
                            tool_calls, [keys[f"observation_{i}"]], [False]
                        )
                    }
                )
            event["tool_calls"] = tool_calls
        elif f"observation_{i}" in keys:
            # A summary / orphan observation (no owning tool call) — surface its text
            # so a compaction reset still reaches the wire.
            event["next_thought"] = _as_text(keys[f"observation_{i}"])
        if event:
            messages.append(event)
    return messages


def arc_history_messages() -> list[dict[str, Any]] | None:
    """Fold the active scope's MATERIALIZED ARC live plane into History messages.

    Resolves the ARC handle + ``(session, scope)`` from the runtime context — the
    same seam the classic ``_RetainingReAct._format_trajectory`` reads — and folds
    ``ARCMemory.render_segments`` (the materialized ``SegmentStore.render``, NEVER a
    re-derivation from the canonical semantic-event log) through
    :func:`segments_to_messages`.

    Returns ``None`` — "ARC is NOT the source; use ReActV2's own internal history" — when
    ARC is disabled, no scope is active, or a read fails (typed reason recorded; no silent
    fallback). Returns the folded list — possibly ``[]`` — when ARC IS the source: ``[]``
    is an ARC-backed but empty plane (first call, before any turn is written), which the
    read seam turns into a static-input HEAD; a non-empty list is the working set, so an
    out-of-band ARC edit propagates. The ``[]``-vs-``None`` split is load-bearing: the
    seam builds the append-only head only when safe (internal history also empty), never
    blanking an in-flight loop's turns on a mid-loop plane wipe.
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.run_keyed_scope(_ctx.active_react_scope())  # #953: per-try ARC partition
    session = _ctx.active_react_session()
    arc = getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
    if arc is None or not scope:
        return None
    try:
        segments = arc.render_segments(session, scope)
        messages = segments_to_messages(segments)
    except Exception as exc:  # noqa: BLE001 - documented no-silent-fallback: record + fall back
        _record_submit_audit(
            REACTV2_ARC_HISTORY_READ_FAILED,
            agent_id=scope,
            field="history",
            text=str(exc),
            suppressed=False,
        )
        return None
    return messages


def override_history_inputs_from_arc(
    inputs: dict[str, Any],
    history_field_name: str,
    input_field_names: tuple[str, ...] = (),
) -> bool:
    """Point ReActV2's History input at the materialized ARC live plane (S2 read seam).

    The adapter-side half of design B, called from
    :meth:`clio_agent.lm.adapters.LenientChatAdapter.format_conversation_history` before
    it delegates to the stock formatter: it replaces ``inputs[history_field_name]`` in
    place with a fresh ``dspy.History`` folded from ARC, so ARC stays the single wire
    source; a no-op (returns ``False``) when ARC is not the source (disabled / no scope /
    read failure — see :func:`arc_history_messages`).

    **The append-only wire fix (#901 — deviation (a) reversed).** The static task inputs
    (``question`` + ``tools``) are folded ONCE into the HEAD history event
    (:func:`_gather_static_inputs` + :func:`_fold_static_inputs_into_head`) and DELETED
    from ``inputs``, so they render once at the front (stock ReActV2 embeds the input in
    the first event) and the adapter's trailing per-call current-input block collapses to
    its byte-static ``main_request`` closing instruction. Every ``self.react`` wire is
    then a strict prefix-extension of the previous beneath that single static tail — the
    append-only invariant the Claude stateful session-delta transport needs
    (:mod:`clio_agent.providers.claude_code_stateful`). The earlier deviation kept the
    question OUT of the prefix as a MOVING trailing block, shifting every tail and forcing
    the detector to (correctly) decline. Server-side content-prefix caching still works
    (the head is byte-stable); the reversal ADDITIONALLY unlocks the structural delta.
    Only fires for a ``dspy.History``-bearing signature (the ReActV2 react signature).

    Args:
        inputs: The adapter's mutable per-call inputs copy (mutated in place).
        history_field_name: The signature's ``dspy.History`` input field name.
        input_field_names: The react signature's (history-removed) input field names —
            the static inputs to embed at the head. When empty (a direct unit call) the
            fallback embeds every non-history key present in ``inputs``.

    Returns:
        ``True`` when the History input was sourced from ARC, else ``False``.
    """
    if not history_field_name or history_field_name not in inputs:
        return False
    messages = arc_history_messages()
    if messages is None:
        return False
    internal = getattr(inputs.get(history_field_name), "messages", None) or []
    if not messages and internal:
        # Empty ARC plane WITH a populated internal history = a mid-loop plane wipe (a full
        # delete op): fall back to ReActV2's own append-only history rather than blank the
        # in-flight turns off the wire. On the first call the internal history is ALSO
        # empty, so this does not fire and the static-input HEAD is synthesized below.
        return False
    static = _gather_static_inputs(inputs, internal, history_field_name, input_field_names)
    messages = _fold_static_inputs_into_head(messages, static)
    for name in static:  # suppress the per-call current-input block for the folded inputs
        inputs.pop(name, None)
    inputs[history_field_name] = dspy.History(messages=messages)
    return True


def _gather_static_inputs(
    inputs: dict[str, Any],
    internal: list[dict[str, Any]],
    history_field_name: str,
    input_field_names: tuple[str, ...],
) -> dict[str, Any]:
    """Resolve the static task inputs to embed ONCE at the append-only head (#901).

    The static inputs (``question``, ``tools``) must render byte-identically at the head
    of EVERY ``self.react`` call. But the stock loop only passes them as current inputs on
    the FIRST call; from call 2 on ``pending_inputs`` is emptied, so ``question`` is no
    longer in ``inputs`` — it lives in the internal ReActV2 history's first event (where
    stock ``_history_event`` folded it). This resolves each declared input field from the
    live ``inputs`` first (``tools`` every call, ``question`` on call 1), else from the
    internal head event 0 (``question`` on call 2+). Same static set on every call ⇒
    byte-stable head ⇒ no first→second boundary reset. ``input_field_names`` empty ⇒ fall
    back to every non-history key in ``inputs`` (a direct unit call).
    """
    names: tuple[str, ...] = input_field_names or tuple(
        n for n in inputs if n != history_field_name
    )
    head0 = internal[0] if internal else {}
    static: dict[str, Any] = {}
    for name in names:
        if name == history_field_name:
            continue
        if name in inputs:
            static[name] = inputs[name]
        elif name in head0:
            static[name] = head0[name]
    return static


def _fold_static_inputs_into_head(
    messages: list[dict[str, Any]], static: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fold the resolved static inputs into the HEAD history event (#901 append-only).

    Each static input is copied into the first event (``setdefault`` — never clobbering a
    real folded value); the caller then DELETES those keys from ``inputs`` so ``format``
    stops emitting a per-call current-input block for them (``adapters/base.py`` l.431-434
    appends the trailing user block only for the inputs that remain; with the static
    inputs gone it collapses to the byte-static closing instruction). When ``messages`` is
    EMPTY (the loop's first call) the head is a SYNTHETIC input-only event, rendered as a
    lone ``user`` message by the read seam's ``format_assistant_message_content``
    suppression — so call 1 is ``[system, {head}, {closing}]`` and call 2 extends it
    append-only. Pure (returns a new list; neither argument is mutated).
    """
    head = dict(messages[0]) if messages else {}
    for name, value in static.items():
        head.setdefault(name, value)
    return [head, *messages[1:]]


# ---- small helpers ---------------------------------------------------------- #


def _turn_count(keys: dict[str, Any]) -> int:
    """Number of react turns in a ``segments_to_keys`` dict (max index + 1).

    ``segments_to_keys`` re-indexes gaplessly from 0, so the count is one past the
    largest ``*_<i>`` suffix; ``{}`` -> 0.
    """
    highest = -1
    for key in keys:
        _, _, suffix = key.rpartition("_")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def _as_text(value: Any) -> str:
    """Coerce a folded field value to ``str`` (a bad write must never raise here)."""
    return value if isinstance(value, str) else str(value if value is not None else "")


def _stringify_value(value: Any) -> str:
    """Compact string form of a submit output value for the audit record."""
    if isinstance(value, str):
        return value
    try:
        import json  # noqa: PLC0415

        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - audit text only; never fail a turn on formatting
        return str(value)
