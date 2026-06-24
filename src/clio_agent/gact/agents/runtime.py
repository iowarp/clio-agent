"""Trajectory-retaining ReAct runtime for the GACT server (#714).

This module owns the *expert runtime engine* carved out of
``clio_agent.gact.app``: the :class:`dspy.ReAct` subclass that retains its
trajectory across a failed final ``extract`` (so the failure can be captured and
repaired) and drives the ARC live-context plane (writing the working-set
trajectory + reading its prompt back from ARC, with proactive auto-compaction).

The retaining subclass is built lazily and cached per ``dspy.ReAct`` base class
(:func:`_retaining_react_cls`) so test fakes that monkeypatch ``dspy.ReAct`` get
a fresh, correct subclass. ``forward`` mirrors the pinned dspy ReAct loop
verbatim, emitting the per-step / per-expert semantic-event highway records and
publishing the retained trajectory *before* ``extract`` runs.

Imports only the shared runtime base (:mod:`clio_agent.gact.runtime`: the
semantic-event funnel + ``gact.context`` boundary + token/context-window leaves)
and stdlib / lazy ``dspy`` -- never ``gact.app`` -- so the dependency graph stays
acyclic. The expert/blueprint *builders* that instantiate this runtime live in
:mod:`clio_agent.gact.agents.builders`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime.context_tokens import (
    _arc_obs_value,
    _autocompact_threshold,
    _last_prompt_tokens,
)
from clio_agent.gact.runtime.globals import (
    _active_lm_last_reasoning,
    _active_semantic_turn_id,
    _emit_expert_lifecycle_event,
    _emit_react_step_event,
    _jsonish,
)

logger = logging.getLogger(__name__)


def _prediction_structured_metadata(result: Any) -> dict[str, Any]:
    return {
        key: getattr(result, key)
        for key in ("workflow_state", "evidence", "artifacts", "errors", "delegation")
        if getattr(result, key, None) not in (None, "")
    }


def _summarize_segments_llm(segments: list[Any]) -> str:
    """Summarize live segments into a compact text that preserves what's needed to
    continue the task. Uses the active expert LM (``dspy.settings.lm``). Returns ''
    on failure (caller then skips compaction and keeps the reactive backstop).
    """
    import dspy  # noqa: PLC0415

    from clio_agent.arc.schema import segment_text  # noqa: PLC0415

    body = "\n".join(segment_text(s) for s in segments)
    sig = dspy.Signature(
        "prior_context -> summary",
        "Summarize the prior reasoning steps, tool calls, and observations into a "
        "compact summary that preserves every fact, result, and decision needed to "
        "continue the task. Be concise but lose no actionable information.",
    )
    try:
        result = dspy.Predict(sig)(prior_context=body)
        return str(getattr(result, "summary", "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.warning("arc auto-compaction summary LLM call failed", exc_info=True)
        return ""


_RETAINING_REACT_CLS_CACHE: dict[Any, Any] = {}


def _retaining_react_cls() -> Any:
    """Build a dspy.ReAct subclass that retains its trajectory.

    Stock ``ReAct.forward`` builds the trajectory locally and discards it if the
    final ``extract`` step raises (e.g. a typed-output ValidationError from a
    model that completed its tool loop but dropped a required output field --
    the common qwopus failure). This subclass publishes the trajectory + the
    extract input_args to ``_ACTIVE_REACT_TRAJECTORY`` *before* calling extract,
    so the failure can be captured AND repaired by re-running only extract.

    NOTE: ``forward`` mirrors ``dspy.predict.react.ReAct.forward`` for the dspy
    pinned in this venv; a unit test guards the Prediction shape. Keep in sync
    if dspy is upgraded.
    """

    import dspy  # noqa: PLC0415

    base = dspy.ReAct
    cached = _RETAINING_REACT_CLS_CACHE.get(base)
    if cached is not None:
        return cached

    class _RetainingReAct(base):  # type: ignore[misc, valid-type]
        # ---- ARC live-context-plane handles (no-op when ARC is disabled) ----

        @staticmethod
        def _arc_scope() -> tuple[Any, str, str]:
            """(ARCMemory, session_id, scope) for the live plane, or (None, '', '')."""
            app = _ctx.active_app()
            scope = _ctx.active_react_scope()
            session = _ctx.active_react_session()
            arc = (
                getattr(getattr(app, "state", None), "arc", None)
                if (app is not None and scope)
                else None
            )
            return arc, session, scope

        @staticmethod
        def _arc_write(
            arc: Any,
            session: str,
            scope: str,
            kind: str,
            content: dict[str, Any],
            idx: int,
            *,
            turn_id: str = "",
            expert_span_id: str = "",
            run_span_id: str = "",
        ) -> None:
            """Append one produced piece to the live plane, stamping the trajectory-
            correlation span ids. Best-effort: a write failure must never break the
            turn (the local trajectory dict is the fallback)."""
            if arc is None:
                return
            try:
                import json as _json  # noqa: PLC0415

                # Approximate per-segment token attribution so the /context breakdown
                # (tokens_by_kind / categories) is populated. Cheap ~4-chars/token
                # heuristic to stay off the hot loop's critical path — the precise
                # window reading is the LM call's prompt_tokens (``used_tokens``).
                tok = max(1, len(_json.dumps(content, default=str)) // 4)
                arc.append_segment(
                    session,
                    scope,
                    kind,
                    content,
                    step=idx,
                    token_count=tok,
                    turn_id=turn_id,
                    expert_span_id=expert_span_id,
                    run_span_id=run_span_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "arc live-plane append failed kind=%s scope=%s",
                    kind,
                    scope,
                    exc_info=True,
                )

        def forward(self, **input_args: Any) -> Any:
            # Install a FRESH trajectory cell (value=None) so a failure inside the
            # loop (before extract) never exposes a stale trajectory from an earlier
            # forward, and a delegated child (its own copied context) gets its own
            # cell rather than publishing into this forward's retained trajectory.
            _ctx.install_trajectory_cell()
            arc, _session, _scope = self._arc_scope()
            if arc is not None:
                # Fresh working context for this react loop: tombstone any prior
                # live WORKING-SET segments in the scope (kept in the store + Trace
                # for replay / as-of-T). A new forward == a new turn's trajectory.
                # Reset via the working-set view (the kind-allowlist render path); the
                # raw LM I/O is the async Trace's job, not a second ARC write, so ARC's
                # buffer holds only the working set (thought / tool_call / observation).
                try:
                    prior = [s.id for s in arc.render_working_set(_session, _scope)]
                    if prior:
                        arc.delete_segments(_session, _scope, prior)
                except Exception:  # noqa: BLE001
                    logger.warning("arc live-plane reset failed scope=%s", _scope, exc_info=True)
            trajectory: dict[str, Any] = {}
            expert_id = str(getattr(self, "_clio_expert_id", "") or "")
            # Correlation: the SEMANTIC turn id is what EVERY existing trajectory /
            # lifecycle write stamps (and what the lm_io seam reads), so all segments
            # in one expert turn share a turn_id — keep them consistent (critic fix).
            turn_id = _active_semantic_turn_id()
            # One span per expert lifecycle so the highway's per-step events of a
            # single expert trajectory can be grouped by consumers.
            expert_span_id = uuid.uuid4().hex[:16]
            # Open the expert lifecycle on the highway. Emitted BEFORE the active
            # span is switched to this expert, so it nests under the delegating
            # scope (the parent expert / the turn).
            _emit_expert_lifecycle_event(
                "expert.lifecycle.started",
                expert_id=expert_id,
                expert_span_id=expert_span_id,
                status="running",
                payload={"input": _jsonish(dict(input_args))},
            )
            # Everything emitted during this expert (ReAct steps, tool.call,
            # lm.call, delegations to children) nests under the expert span; the
            # context propagates into delegated children via copy_context().
            parent_token = _ctx.set_parent_span(expert_span_id)
            try:
                max_iters = input_args.pop("max_iters", self.max_iters)
                for idx in range(max_iters):
                    # One span per ReAct Step: while it is active, this step's
                    # lm.call (self.react) and tool.call (the act/observe) auto-nest
                    # under it, so the raw LLM I/O + tool calling are correlated to
                    # the step. Reset to the expert span at the step boundary.
                    step_span_id = uuid.uuid4().hex[:16]
                    step_token = _ctx.set_parent_span(step_span_id)
                    try:
                        try:
                            pred = self._call_with_potential_trajectory_truncation(
                                self.react, trajectory, **input_args
                            )
                        except ValueError:
                            # Agent failed to select a valid tool; end the loop and
                            # let extract work with whatever trajectory exists so far.
                            break

                        # Capture the raw reasoning channel for THIS step now —
                        # before any tool runs (a delegation tool's child LM call
                        # would otherwise overwrite history[-1]).
                        step_reasoning = _active_lm_last_reasoning()
                        trajectory[f"thought_{idx}"] = pred.next_thought
                        trajectory[f"tool_name_{idx}"] = pred.next_tool_name
                        trajectory[f"tool_args_{idx}"] = pred.next_tool_args
                        # ARC live-plane writes: thought + tool_call are known now;
                        # the observation is written after the tool runs. No-op when
                        # ARC is disabled (arc is None). Each write is correlation-
                        # stamped (turn / expert / step) so the working-set trajectory
                        # is grouped by turn (Q3 reads these correlation ids).
                        self._arc_write(
                            arc,
                            _session,
                            _scope,
                            "thought",
                            {"text": pred.next_thought},
                            idx,
                            turn_id=turn_id,
                            expert_span_id=expert_span_id,
                            run_span_id=step_span_id,
                        )
                        self._arc_write(
                            arc,
                            _session,
                            _scope,
                            "tool_call",
                            {"name": pred.next_tool_name, "args": pred.next_tool_args},
                            idx,
                            turn_id=turn_id,
                            expert_span_id=expert_span_id,
                            run_span_id=step_span_id,
                        )
                        try:
                            trajectory[f"observation_{idx}"] = self.tools[pred.next_tool_name](
                                **pred.next_tool_args
                            )
                        except Exception as err:  # noqa: BLE001 - mirror dspy: errors become observations
                            trajectory[f"observation_{idx}"] = (
                                f"Execution error in {pred.next_tool_name}: {err}"
                            )
                        self._arc_write(
                            arc,
                            _session,
                            _scope,
                            "observation",
                            {"text": _arc_obs_value(trajectory[f"observation_{idx}"])},
                            idx,
                            turn_id=turn_id,
                            expert_span_id=expert_span_id,
                            run_span_id=step_span_id,
                        )

                        # Put this ReAct Step (LLM response + tool act/observe) on
                        # the highway with FULL content BEFORE the loop discards
                        # everything but the final extract. Pin its parent to the
                        # expert span (the step event is a child of the expert, not
                        # self-parented under the active step span).
                        _emit_react_step_event(
                            expert_id=expert_id,
                            expert_span_id=expert_span_id,
                            step_span_id=step_span_id,
                            step_index=idx,
                            thought=pred.next_thought,
                            reasoning=step_reasoning,
                            tool_name=pred.next_tool_name,
                            tool_args=pred.next_tool_args,
                            observation=trajectory[f"observation_{idx}"],
                            is_finish=pred.next_tool_name == "finish",
                        )

                        if pred.next_tool_name == "finish":
                            break
                    finally:
                        _ctx.reset(step_token)

                # Publish BEFORE extract: a failed extract still exposes the trajectory.
                # Mutates the cell installed at the top of this forward (a
                # reassignment without a token reset, as before).
                _ctx.publish_trajectory(
                    {"trajectory": dict(trajectory), "input_args": dict(input_args)}
                )
                extract = self._call_with_potential_trajectory_truncation(
                    self.extract, trajectory, **input_args
                )
                extract_reasoning = _active_lm_last_reasoning()
                final_pred = dspy.Prediction(trajectory=trajectory, **extract)
                # Close the expert lifecycle with the extract output — the typed
                # result that returns to the parent. FULL/uncapped on the highway;
                # the parent's filter to just this is a downstream projection.
                _emit_expert_lifecycle_event(
                    "expert.extract.completed",
                    expert_id=expert_id,
                    expert_span_id=expert_span_id,
                    status="completed",
                    payload={
                        "output": str(getattr(final_pred, "answer", "") or ""),
                        "reasoning": extract_reasoning,
                        "structured": _prediction_structured_metadata(final_pred),
                        "step_count": sum(1 for k in trajectory if k.startswith("tool_name_")),
                    },
                )
                return final_pred
            finally:
                _ctx.reset(parent_token)

        def _format_trajectory(self, trajectory: dict[str, Any]) -> str:
            """THE live-plane read seam. Render the prompt's trajectory from ARC, not
            the local dict. Reuses stock's formatter verbatim (so the output is
            byte-identical given the same keys) but feeds it ARC's render_keys — so
            edits (delete/summarize/insert/append) on ARC change the next prompt.
            Falls back to stock when ARC is disabled. Runs for both ``self.react``
            (loop) and ``self.extract`` (final) — extract sees the same compacted
            view, which is what we want.
            """
            arc, session, scope = self._arc_scope()
            if arc is None:
                return super()._format_trajectory(trajectory)
            arc_keys = arc.render_segments_keys(session, scope)
            return super()._format_trajectory(arc_keys)

        def _call_with_potential_trajectory_truncation(
            self, module: Any, trajectory: dict[str, Any], **input_args: Any
        ) -> Any:
            """Proactive, scope-aware 90% auto-compaction fires BEFORE every send;
            stock's reactive truncate_trajectory stays as the never-fired backstop."""
            self._maybe_autocompact()
            return super()._call_with_potential_trajectory_truncation(
                module, trajectory, **input_args
            )

        def _maybe_autocompact(self) -> None:
            arc, session, scope = self._arc_scope()
            if arc is None:
                return
            window = _ctx.active_react_context_window()
            last = _last_prompt_tokens()  # provider-exact prompt_tokens of the last call
            if not window or not last:
                return
            ratio = last / window
            if ratio < _autocompact_threshold():
                return
            # Compact via the working-set view (the kind-allowlist render path). ARC's
            # buffer is the working set only (thought / tool_call / observation), so
            # this folds exactly the live trajectory into one summary.
            live = arc.render_working_set(session, scope)
            if len(live) <= 1:
                return  # nothing meaningful to compact yet
            summary = _summarize_segments_llm(live)
            if not summary:
                return  # summary LLM failed; leave context, reactive backstop remains
            arc.summarize_segments(session, scope, [s.id for s in live], {"text": summary})
            logger.info(
                "arc auto-compaction scope=%s ratio=%.2f>=%.2f replaced=%d window=%d last=%d",
                scope,
                ratio,
                _autocompact_threshold(),
                len(live),
                window,
                last,
            )

    _RETAINING_REACT_CLS_CACHE[base] = _RetainingReAct
    return _RetainingReAct
