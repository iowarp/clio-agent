"""``draft_alternatives`` and the human judge: BestOfN / Refine on demand (Phase 9).

The agent calls ``draft_alternatives(n, rubric, strategy, judge)`` when a request is
worth several tries ("write the email to Dana"). Each try is the agent itself on the
same task -- a real :class:`ClioReAct` forward, minus the tools a try cannot use -- on
its own clio-core scope ``<agent>#run<k>``, forked from the conversation as it stood
before this turn. The run is recorded in clio-core (:mod:`variant_records`) and shown
live (:mod:`variant_events`); the user is told, by an injection, that alternatives are
being drafted.

Selection is DSPy's, never clio's:

* ``best_of_n`` -- the ``n`` tries run in parallel (``dspy.Parallel``); then
  ``dspy.BestOfN`` selects over them by reward, with the very rollout ids and
  temperature it gives its own tries (:class:`_Replay` hands it each finished try).
  ``judge: lm`` -- the reward is the compiled LM judge over ``rubric``;
  ``judge: user`` -- the reward is the user's pick (1.0 for it, 0.0 for the others).
* ``refine`` with ``judge: lm`` -- ``dspy.Refine`` over the tries (sequential by
  definition: each needs the previous one's feedback), as a blueprint declares it.
* ``refine`` with ``judge: user`` -- one try, then the user picks and comments; the
  comment is the advice (a recorded ``variant_advice`` injection) for another try
  forked from the pick, until the user accepts (a pick without a comment) or ``n``
  tries exist. The user's accept is the reward that reaches the threshold.

A human judge is a pause: the turn yields a ``choice`` question (the drafts' ids and
final texts); the run's state is in clio-core and the session row points at it, so the
answer -- even after a restart -- resumes it at the agent's next forward
(:func:`resume_pending`). The selected try's line then continues the conversation:
this turn's own steps on the base scope are retired and the try's line is appended.
A run that ends without a pick -- a new turn first, a dismissed or expired question --
is closed as such (:mod:`variant_close`).
"""

from __future__ import annotations

import contextlib
import functools
import threading
import uuid
from collections.abc import Callable, Mapping
from contextvars import Context, copy_context
from typing import Any

import dspy

from clio_agent import conf
from clio_agent.errors import ClioError, MCPProtocolError
from clio_agent.gact import context as _ctx
from clio_agent.gact.agents import variant_lines
from clio_agent.gact.agents.variant_close import PENDING_META, close_for_question, latest_question
from clio_agent.gact.agents.variant_events import emit_selected, emit_try, try_context
from clio_agent.gact.agents.variant_records import TryRecord, VariantRun, load_run, save_run

__all__ = [
    "DRAFT_TOOL",
    "DraftAlternativesError",
    "UserJudgedVariant",
    "VariantDraftsFailed",
    "build_draft_alternatives_tool",
    "capped_n",
    "max_n",
    "new_variants_id",
    "resume_pending",
    "variant_answer_problem",
]

DRAFT_TOOL = "draft_alternatives"
DRAFTING_SOURCE = "variant_drafting"
ADVICE_SOURCE = "variant_advice"
DEFAULT_MAX_N = 4
#: What a try cannot do: draft again, ask the user, hand back a plan (``submit`` is
#: the try's own, rebuilt by its loop).
_NOT_IN_TRIES = frozenset({"submit", DRAFT_TOOL, "ask_user", "plan_exit"})
_STRATEGIES = frozenset({"best_of_n", "refine"})
_JUDGES = frozenset({"lm", "user"})


class DraftAlternativesError(ClioError):
    """``draft_alternatives`` was called in a way it cannot run (the model is told why)."""

    reason = "draft_alternatives_refused"

    def __init__(self, detail: str) -> None:
        super().__init__(f"draft_alternatives refused: {detail}", error_type=self.reason)


class VariantDraftsFailed(ClioError):
    """No try of a run produced a candidate."""

    reason = "variant_drafts_failed"

    def __init__(self, run: VariantRun) -> None:
        errors = "; ".join(f"try {t.try_index}: {t.error}" for t in run.tries if t.error)
        super().__init__(
            f"every try of variant run {run.variants_id} failed ({errors or 'no detail'})",
            error_type=self.reason,
            details={"variants_id": run.variants_id},
        )


def new_variants_id() -> str:
    """A fresh, stable id for one run."""
    return f"var_{uuid.uuid4().hex[:16]}"


def max_n() -> int:
    """The most tries one run may have (``variants.max_n``, default 4)."""
    value = conf.resolve(
        "variants.max_n", env="CLIO_VARIANTS_MAX_N", default=DEFAULT_MAX_N, cast=conf.as_int
    )
    if value < 1:
        raise DraftAlternativesError(f"variants.max_n must be >= 1, got {value}")
    return value


def capped_n(n: int) -> int:
    """``n`` within the configured cap."""
    return min(int(n), max_n())


def try_module(agent: Any) -> Any:
    """The agent as one try runs it: same signature, tools and step cap, minus the tools
    a try cannot use. A non-loop module is its own try."""
    from clio_agent.gact.agents.clio_react import ClioReAct  # noqa: PLC0415

    if not isinstance(agent, ClioReAct):
        return agent
    tools = [tool for name, tool in agent.tools.items() if name not in _NOT_IN_TRIES]
    module = ClioReAct(agent.signature, tools=tools, max_iters=agent.max_iters)
    module._clio_expert_id = getattr(agent, "_clio_expert_id", "")
    return module


class _TryFailed(RuntimeError):
    """A try recorded as failed, rebuilt from clio-core (after a pause)."""


class _Replay(dspy.Module):
    """``dspy.BestOfN``'s module over tries that already ran.

    BestOfN calls it once per rollout id (``start + i``, the ids each try ran with); it
    returns that try's prediction, or raises its error, so BestOfN's own loop -- reward,
    threshold, first-best tie rule, failure count -- selects. ``served`` is the order
    BestOfN asked in (the reward reads the try being scored from it).
    """

    def __init__(self, outcomes: Mapping[int, Any], start: int, served: list[int]) -> None:
        super().__init__()
        self.outcomes = outcomes
        self.start = start
        self.served = served
        self.lm: Any = None

    def get_lm(self) -> Any:
        """The rollout copy BestOfN bound (``None`` before)."""
        return self.lm

    def set_lm(self, lm: Any) -> None:
        """Bind BestOfN's per-rollout LM copy: its ``rollout_id`` names the try."""
        self.lm = lm

    def deepcopy(self) -> "_Replay":
        """The tries already ran: a copy shares their (immutable) outcomes."""
        return _Replay(self.outcomes, self.start, self.served)

    def forward(self, **kwargs: Any) -> Any:
        """The finished try for this rollout."""
        index = int(self.lm.kwargs["rollout_id"]) - self.start
        self.served.append(index)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Drafts:
    """The tries of one run: forked, run, recorded, and selected by DSPy."""

    def __init__(self, run: VariantRun, module: Any, inputs: Mapping[str, Any], lm: Any) -> None:
        self.app = _ctx.active_app()
        self.run = run
        self.module = module
        self.inputs = dict(inputs)
        self.lm = lm
        self.start = int((getattr(lm, "kwargs", None) or {}).get("rollout_id", 0) or 0)
        self.outcomes: dict[int, Any] = {}
        self.lock = threading.Lock()
        for record in run.tries:  # a resumed run: its earlier tries as DSPy saw them
            if record.status == "completed":
                self.outcomes[record.try_index] = dspy.Prediction(answer=record.text)
            else:
                self.outcomes[record.try_index] = _TryFailed(record.error)

    def save(self, record: TryRecord | None = None) -> None:
        """Record the run's state on clio-core (and a try's change on the highway)."""
        with self.lock:
            save_run(self.app, self.run)
        if record is not None:
            emit_try(self.run, record)

    def one_try(
        self,
        try_index: int,
        *,
        source: str = "",
        cut: str = "",
        keep_prefix: int | None = None,
        advice: str = "",
        forked_from: int | None = None,
    ) -> Any:
        """Run try ``try_index`` on its own scope and record what it produced."""
        record = TryRecord(
            try_index=try_index,
            scope=variant_lines.try_scope(try_index),
            forked_from=forked_from,
            advice=advice,
        )
        with self.lock:
            self.run.add_try(record)
        self.save(record)
        forked: list[str] = []
        try:
            try:
                with try_context(self.run, record):
                    forked = variant_lines.fork_try(try_index, source=source, cut_id=cut)
                    record.prefix_ids = forked if keep_prefix is None else forked[:keep_prefix]
                    if advice:
                        variant_lines.record_advice(try_index, advice, ADVICE_SOURCE)
                    module = self.module.deepcopy()
                    lm = self.lm.copy(rollout_id=self.start + try_index, temperature=1.0)
                    module.set_lm(lm)
                    from clio_agent.gact.agents.clio_react import (  # noqa: PLC0415
                        continuing_fork,
                    )

                    with continuing_fork() if source else contextlib.nullcontext():
                        pred = module(**self.inputs)
            finally:
                # The try's tab shows its own steps: a refine try's copy of the pick's
                # line (part of its line to commit) is not among them.
                record.segment_ids = record.segment_ids[len(forked) - len(record.prefix_ids) :]
        except Exception as exc:
            record.status, record.error = "failed", f"{type(exc).__name__}: {exc}"
            self.outcomes[try_index] = exc
            self.save(record)
            raise
        record.status, record.text = "completed", str(getattr(pred, "answer", "") or "")
        self.outcomes[try_index] = pred
        self.save(record)
        return pred

    def parallel(self, count: int, *, cut: str) -> None:
        """Run ``count`` independent tries at once (``dspy.Parallel``), each in a copy of
        this context; a failed try is recorded and the others go on."""

        def in_own_context(context: Context, **example: Any) -> Any:
            return context.run(self.one_try, cut=cut, **example)

        calls = [
            (functools.partial(in_own_context, copy_context()), {"try_index": k})
            for k in range(count)
        ]
        # timeout=0: no wall-clock straggler re-run (a slow try is a long agent run, not
        # a stuck one; re-running it would double its cost on the same scope).
        dspy.Parallel(
            num_threads=count, max_errors=count + 1, timeout=0, disable_progress_bar=True
        )(calls)
        self.escalate()

    def escalate(self) -> None:
        """A cancelled turn or a terminal MCP refusal in any try ends the run as itself."""
        from clio_agent.gact.runtime.globals import _TurnCancelled  # noqa: PLC0415

        for outcome in self.outcomes.values():
            if isinstance(outcome, (_TurnCancelled, MCPProtocolError)):
                raise outcome

    def select(self, reward: Callable[[int, dict, Any], float], threshold: float) -> int:
        """``dspy.BestOfN`` over the finished tries; the index of the one it returns."""
        served: list[int] = []

        def scored(kwargs: dict, pred: Any) -> float:
            return reward(served[-1], kwargs, pred)

        replay = _Replay(self.outcomes, self.start, served)
        count = max(self.outcomes) + 1
        try:
            with dspy.context(lm=self.lm):
                best = dspy.BestOfN(module=replay, N=count, reward_fn=scored, threshold=threshold)(
                    **self.inputs
                )
            if best is None:  # BestOfN returns None when every try failed (N <= 2)
                raise VariantDraftsFailed(self.run)
        except Exception:
            self.run.status = "failed"
            self.save()
            raise
        return next(i for i, out in self.outcomes.items() if out is best)

    def select_by_judge(self, spec: Any) -> int:
        """BestOfN with the compiled LM judge as its reward; every score is recorded."""
        from clio_agent.gact.agents.module_variants import compile_reward_fn  # noqa: PLC0415

        judge = compile_reward_fn(spec, agent_id=self.run.agent_id)

        def reward(index: int, kwargs: dict, pred: Any) -> float:
            score = float(judge(kwargs, pred))
            record = self.run.try_at(index)
            record.score = score
            self.save(record)
            return score

        return self.select(reward, spec.threshold)

    def select_pick(self, pick: int) -> int:
        """BestOfN with the user's pick as its reward (1.0 for it, 0.0 for the others)."""

        def reward(index: int, kwargs: dict, pred: Any) -> float:
            return 1.0 if index == pick else 0.0

        return self.select(reward, 1.0)


def _judge_spec(run: VariantRun) -> Any:
    from clio_agent.gact.runtime.type_parsing import parse_module_variant  # noqa: PLC0415

    module = {
        "variant": run.strategy,
        "n": run.n,
        "threshold": 1.0,
        "reward": {"instructions": run.rubric, "inputs": ["question"]},
    }
    return parse_module_variant(module, agent_id=run.agent_id)


def _mark_selected(app: Any, run: VariantRun, index: int) -> TryRecord:
    run.selected_index, run.status = index, "selected"
    save_run(app, run)
    emit_selected(run)
    return run.try_at(index)


def _drafting_note(run: VariantRun) -> str:
    who = "you pick one" if run.judge == "user" else "the best is selected against the rubric"
    if run.strategy == "refine":
        what = f"one draft at a time, refined up to {run.n} drafts"
    else:
        what = f"{run.n} alternatives in parallel"
    capped = f" ({run.n_requested} asked, capped at {run.n})" if run.n_requested > run.n else ""
    return f"Drafting {what}{capped}; {who}. Rubric: {run.rubric}"


def _pause(app: Any, run: VariantRun) -> None:
    """Yield the drafts to the user as a ``choice`` question (surfaced after the turn)."""
    from clio_agent.gact.artifacts.observer_bridge import observer_call_id  # noqa: PLC0415
    from clio_agent.gact.ask_user_tool import (  # noqa: PLC0415
        PENDING_ASK_USER_META,
        ask_user_expires_at,
    )
    from clio_agent.gact.permission_delivery import attended_session_id  # noqa: PLC0415

    candidates = [t for t in run.tries if t.status == "completed"]
    if not candidates:
        run.status = "failed"
        save_run(app, run)
        raise VariantDraftsFailed(run)
    refinable = run.strategy == "refine" and len(run.tries) < run.n
    prompt = "Which draft should continue the conversation?" + (
        " Add a comment to have it refined; pick without a comment to accept it."
        if refinable
        else ""
    )
    sid = _ctx.active_session_id()
    run.status = "awaiting_pick"
    save_run(app, run)
    pending = {
        "action": "ask_user",
        "question": prompt,
        "kind": "choice",
        "choices": [
            {"label": f"Draft {t.try_index + 1}", "value": t.scope, "description": t.text}
            for t in candidates
        ],
        "allow_freeform": True,
        "reason": f"Pick the draft that best meets: {run.rubric}",
        # No default lifetime (as ask_user): only the window the agent asked for.
        "expires_at": ask_user_expires_at(run.pick_expires_in_s),
        "owner_session_id": sid,
        "attended_session_id": attended_session_id(app, sid),
        "tool_name": DRAFT_TOOL,
        "invocation_id": observer_call_id() or f"{run.variants_id}:{len(run.tries)}",
        "caller": {"agent_id": run.agent_id},
        "surfaced": False,
        "metadata": {
            "variants_id": run.variants_id,
            "variant": {
                "strategy": run.strategy,
                "judge": run.judge,
                "n": run.n,
                "rubric": run.rubric,
                "refinable": refinable,
                "candidates": [
                    {"id": t.scope, "try_index": t.try_index, "text": t.text} for t in candidates
                ],
            },
        },
    }
    app.state.sessions.update(
        sid,
        metadata_patch={
            PENDING_ASK_USER_META: pending,
            PENDING_META: {"variants_id": run.variants_id, "agent_id": run.agent_id},
        },
    )


def _commit(app: Any, run: VariantRun, index: int) -> TryRecord:
    """The selected try's line continues the conversation on the base scope."""
    record = _mark_selected(app, run, index)
    variant_lines.record_winner(record.try_index, record.prefix_ids, cut_id=run.base_cut_id)
    return record


class _Settle:
    """What the loop does once the ``draft_alternatives`` step is recorded."""

    def __init__(self, app: Any, run: VariantRun | None) -> None:
        self.app = app
        self.run = run

    def settle(self) -> str | None:
        """Commit the selected line and return its answer; ``None``: the user picks."""
        if self.run is None:
            return None
        assert self.run.selected_index is not None
        record = self.run.try_at(self.run.selected_index)
        variant_lines.record_winner(
            record.try_index, record.prefix_ids, cut_id=self.run.base_cut_id
        )
        return record.text


def _lm_refine(run: VariantRun, module: Any, inputs: Mapping[str, Any], lm: Any) -> VariantRun:
    """``dspy.Refine`` over the tries (clio writes its advice for a ClioReAct inner)."""
    from clio_agent.gact.agents.module_variants import (  # noqa: PLC0415
        _RunKeyedModule,
        _RunScopedRefine,
        compile_reward_fn,
    )

    spec = _judge_spec(run)
    wrapped = _RunScopedRefine(
        module=_RunKeyedModule(module, agent_id=run.agent_id, variant="refine"),
        N=run.n,
        reward_fn=compile_reward_fn(spec, agent_id=run.agent_id),
        threshold=spec.threshold,
    )
    wrapped._clio_variant = "refine"
    wrapped._clio_agent_id = run.agent_id
    wrapped._clio_origin = run.origin
    wrapped._clio_rubric = run.rubric
    wrapped._clio_fork_cut = run.base_cut_id
    wrapped._clio_commit = False
    wrapped._clio_n_requested = run.n_requested
    with dspy.context(lm=lm):
        pred = wrapped(**inputs)
    return load_run(_ctx.active_app(), run.session_id, pred.variant_selection["variants_id"])


def _draft(n: int, rubric: str, strategy: str, judge: str, expires_in_s: int = 0) -> str:
    from clio_agent.gact.agents.clio_react import active_loop  # noqa: PLC0415
    from clio_agent.gact.artifacts.observer_bridge import observer_call_id  # noqa: PLC0415
    from clio_agent.gact.injection_parts import emit_injection  # noqa: PLC0415

    loop = active_loop()
    if loop is None or _ctx.active_react_run() >= 0:
        raise DraftAlternativesError("only the agent's own turn can draft alternatives")
    if strategy not in _STRATEGIES or judge not in _JUDGES:
        raise DraftAlternativesError(
            f"strategy must be one of {sorted(_STRATEGIES)} and judge one of {sorted(_JUDGES)}"
        )
    if n < 1 or not rubric.strip():
        raise DraftAlternativesError(
            "n must be >= 1 and the rubric must say what makes a good draft"
        )
    if expires_in_s < 0:
        raise DraftAlternativesError("expiresInSeconds must be >= 0 (0: no deadline)")
    if not loop.variant_claim.acquire(blocking=False):
        raise DraftAlternativesError("this turn is already drafting alternatives")
    try:
        app = _ctx.active_app()
        run = VariantRun(
            variants_id=new_variants_id(),
            session_id=_ctx.active_react_session(),
            agent_id=_ctx.active_react_scope(),
            turn_id=_ctx.active_turn_id(),
            origin=DRAFT_TOOL,
            strategy=strategy,
            judge=judge,
            n=capped_n(n),
            n_requested=n,
            rubric=rubric.strip(),
            threshold=1.0 if judge == "lm" else None,
            base_cut_id=loop.recorder.head_id,
            pick_expires_in_s=expires_in_s,
        )
        emit_injection(
            DRAFTING_SOURCE, _drafting_note(run), call_id=observer_call_id(), agent_id=run.agent_id
        )
        module = try_module(loop.agent)
        if judge == "lm" and strategy == "refine":
            run = _lm_refine(run, module, loop.inputs, loop.lm)
            loop.variant_outcome = _Settle(app, run)
            return _selected_result(run)
        save_run(app, run)
        drafts = _Drafts(run, module, loop.inputs, loop.lm)
        if strategy == "best_of_n":
            drafts.parallel(run.n, cut=run.base_cut_id)
        else:
            drafts.one_try(0, cut=run.base_cut_id)
        if judge == "lm":
            _mark_selected(app, run, drafts.select_by_judge(_judge_spec(run)))
            loop.variant_outcome = _Settle(app, run)
            return _selected_result(run)
        _pause(app, run)
        loop.variant_outcome = _Settle(app, None)
    except BaseException:
        loop.variant_claim.release()  # a refused or failed draft leaves the turn free
        raise
    ready = sum(1 for t in run.tries if t.status == "completed")
    return (
        f"{ready} draft(s) are shown to the user, who picks one. END YOUR TURN now: the "
        "conversation continues from the user's pick."
    )


def _selected_result(run: VariantRun) -> str:
    assert run.selected_index is not None
    chosen = run.try_at(run.selected_index)
    score = "" if chosen.score is None else f" (score {chosen.score:.2f})"
    return (
        f"Draft {chosen.try_index + 1} of {len(run.tries)} was selected{score}. It is the "
        "answer the user gets; the turn ends with it."
    )


def build_draft_alternatives_tool() -> Any:
    """The ``draft_alternatives`` agent tool."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def draft_alternatives(
        n: int,
        rubric: str,
        strategy: str = "best_of_n",
        judge: str = "user",
        expiresInSeconds: int = 0,  # noqa: N803 - public tool schema is camelCase (ask_user's)
    ) -> str:
        """Draft several alternative answers to the user's request and keep the best.

        Use it when a request is worth several tries (an email, a summary, a plan) or
        the user asks for alternatives. Each draft is you doing the same task, apart,
        from the conversation as it was before this turn. ``strategy``: ``best_of_n``
        drafts ``n`` alternatives at once; ``refine`` drafts one and improves it with
        feedback, up to ``n`` drafts. ``judge``: ``user`` shows the drafts to the user,
        who picks one (and may comment to refine it); ``lm`` scores them against
        ``rubric``. The selected draft is your answer and the turn ends. If the user
        sends a new message instead of picking, the drafts are dropped.
        """
        return _draft(
            int(n),
            str(rubric or ""),
            str(strategy or ""),
            str(judge or ""),
            int(expiresInSeconds or 0),
        )

    return native_tool(
        draft_alternatives,
        name=DRAFT_TOOL,
        presentation="text",
        domain="interaction",
        desc=draft_alternatives.__doc__,
        title="Draft alternatives",
        args={
            "n": {"type": "integer", "description": "How many drafts (capped by config)."},
            "rubric": {"type": "string", "description": "What makes a draft good."},
            "strategy": {
                "type": "string",
                "description": "best_of_n (parallel drafts) or refine (improve one draft).",
            },
            "judge": {
                "type": "string",
                "description": "user (the user picks) or lm (scored against the rubric).",
            },
            "expiresInSeconds": {
                "type": "integer",
                "description": (
                    "judge user only: optional window in seconds for the pick. Omit or 0: "
                    "the drafts wait until the user picks, dismisses them or moves on."
                ),
            },
        },
    )


class UserJudgedVariant(dspy.Module):
    """A blueprint or spawn-strategy variant whose judge is the user.

    Not a DSPy loop: the tries run (in parallel for ``best_of_n``, one for ``refine``),
    then the agent's turn yields them to the user; the answer resumes it
    (:func:`resume_pending`), and DSPy's BestOfN selects with the pick as its reward.
    """

    def __init__(self, inner: dspy.Module, spec: Any, *, agent_id: str) -> None:
        super().__init__()
        self.inner = inner
        self.spec = spec
        self._clio_agent_id = agent_id

    def get_lm(self) -> Any:
        """The inner program's LM."""
        return self.inner.get_lm()

    def set_lm(self, lm: Any) -> None:
        """Bind the inner program to ``lm``."""
        self.inner.set_lm(lm)

    def forward(self, **kwargs: Any) -> Any:
        """Resume an answered run, or draft and yield the tries to the user."""
        from clio_agent.gact.injection_parts import emit_injection  # noqa: PLC0415

        resumed = resume_pending(self.inner, kwargs)
        if resumed is not None:
            return resumed
        app = _ctx.active_app()
        run = VariantRun(
            variants_id=new_variants_id(),
            session_id=_ctx.active_react_session(),
            agent_id=_ctx.active_react_scope(),
            turn_id=_ctx.active_turn_id(),
            origin="module_variant",
            strategy=self.spec.variant,
            judge="user",
            n=self.spec.n,
            n_requested=self.spec.n,
            rubric=self.spec.reward_instructions,
        )
        save_run(app, run)
        emit_injection(DRAFTING_SOURCE, _drafting_note(run), agent_id=run.agent_id)
        lm = self.inner.get_lm() or dspy.settings.lm
        drafts = _Drafts(run, try_module(self.inner), kwargs, lm)
        if run.strategy == "best_of_n":
            drafts.parallel(run.n, cut="")
        else:
            drafts.one_try(0)
        _pause(app, run)
        return dspy.Prediction(answer="", termination_reason="variant_pick_yield")


def _clear_pending(app: Any, sid: str) -> None:
    app.state.sessions.update(sid, metadata_patch={PENDING_META: {}})


def _pick(run: VariantRun, question: Any) -> int:
    """The try the user picked (typed error when the answer names none of the drafts)."""
    chosen = list(question.selected_options or [])[:1]
    for record in run.tries:
        if chosen and record.scope == chosen[0] and record.status == "completed":
            return record.try_index
    raise DraftAlternativesError(f"the answer picks no draft of run {run.variants_id}: {chosen}")


def resume_pending(agent: Any, inputs: Mapping[str, Any]) -> dspy.Prediction | None:
    """Take up this agent's human-judged run once the user answered its question.

    ``None`` when there is nothing to resume (no run waits on this agent, or its
    question is still open, or it was dismissed or expired -- the run is closed as such,
    if the close did not happen yet): the forward runs as usual. Otherwise the answer is
    recorded
    on the run; a Refine comment (below ``n`` tries) runs another try forked from the
    pick and yields again; else BestOfN selects the pick and its line continues the
    conversation -- the forward's answer is the pick's text, with no model call.
    """
    if _ctx.active_react_run() >= 0:
        return None  # a try never resumes anything
    app, sid = _ctx.active_app(), _ctx.active_session_id()
    sessions = getattr(getattr(app, "state", None), "sessions", None)
    row = sessions.get(sid) if sessions is not None and sid else None
    pending = (getattr(row, "metadata", None) or {}).get(PENDING_META)
    if not isinstance(pending, Mapping) or not pending.get("variants_id"):
        return None
    if pending.get("agent_id") != _ctx.active_react_scope():
        return None
    run = load_run(app, _ctx.active_react_session() or sid, str(pending["variants_id"]))
    question = latest_question(app, run.variants_id)
    if question is None or question.status == "pending":
        return None
    if question.status != "answered":  # cancelled or expired: closed without a pick
        close_for_question(app, question)
        return None
    pick = _pick(run, question)
    comment = str(question.answer or "").strip()
    run.pick, run.comment, run.question_id, run.status = pick, comment, question.id, "answered"
    lm = agent.get_lm() or dspy.settings.lm
    drafts = _Drafts(run, try_module(agent), inputs, lm)
    drafts.save()
    if run.strategy == "refine" and comment and len(run.tries) < run.n:
        source = run.try_at(pick)
        drafts.one_try(
            len(run.tries),
            source=source.scope,
            keep_prefix=len(source.prefix_ids),
            advice=comment,
            forked_from=pick,
        )
        _pause(app, run)
        return dspy.Prediction(answer="", termination_reason="variant_pick_yield")
    record = _commit(app, run, drafts.select_pick(pick))
    _clear_pending(app, sid)
    return dspy.Prediction(
        answer=record.text,
        termination_reason="variant_selected",
        variant_selection={
            "variants_id": run.variants_id,
            "variant": run.strategy,
            "judge": "user",
            "winning_index": record.try_index,
            "pick": pick,
            "comment": comment,
        },
    )


def variant_answer_problem(question: Any, selected_options: list[str]) -> str | None:
    """Why an answer to a drafts question is invalid (``None``: it is valid).

    The answer is ``selected_options: [<draft id>]`` (the pick) plus an optional
    ``answer`` text (the comment).
    """
    if not (getattr(question, "metadata", None) or {}).get("variants_id"):
        return None
    if len([s for s in selected_options if s]) != 1:
        return "pick exactly one draft (selected_options: [<draft id>]); the answer is a comment"
    return None
