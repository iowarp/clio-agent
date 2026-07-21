"""Declarable ``dspy.BestOfN`` / ``dspy.Refine`` module variants (#948 S5).

A blueprint widens its ``module`` declaration from ``{kind}`` to
``{kind, variant, n, threshold, reward}``. When a variant is declared,
:func:`wrap_module_variant` wraps the inner DSPy program (the ``predict`` /
``chain_of_thought`` / ``react`` module the dispatch already built) in a REAL
``dspy.BestOfN`` / ``dspy.Refine`` (the installed engine's loop, not a
re-implementation), whose ``reward_fn`` is a source-backed generated ``def`` that
scores each attempt via an LM-as-judge ``dspy.Predict`` over the declared reward
signature.

Two clio-specific concerns are handled here, in this owner module (never accreted
onto ``builders.py``):

1. **In-process run keying.** ``dspy.BestOfN``/``Refine`` run N forwards of ONE
   module in ONE child session. Even sequentially those tries collide on the ARC
   live plane + transcript-tap KEYS — try N's ``arc_history_messages`` fold would
   accumulate try N-1's trajectory (a model-INPUT correctness bug). A per-call
   ledger (:data:`_LEDGER`) hands each try a monotonic ``run_index``; the wrapper
   sets :func:`clio_agent.gact.context.set_react_run` around the inner forward so the
   keying planes partition per try via ``run_keyed_scope`` — while
   ``active_react_scope`` stays the bare agent id for every attribution reader
   (#953 spike verdict).

2. **``inspect.getsource`` for Refine.** ``dspy.Refine.__init__`` calls
   ``inspect.getsource`` on the module class AND the reward fn. The reward fn is a
   real nested ``def`` defined in THIS file (source-backed), and the inner program is
   interposed behind :class:`_RunKeyedModule` (also defined here), so both getsource
   calls resolve against this module's on-disk source — ``Refine`` composes with any
   inner kind, ``react`` included.

Observability: every try emits a structured ``variant.try`` / ``variant.reward`` log
carrying the run index and score; the winning try's index + score are stamped, typed
and additive, onto the returned ``Prediction`` as ``variant_selection`` (CHANGELOG'd).
No silent retries — a reward parse failure logs a typed ``variant.reward.parse_failed``
and scores ``0.0`` rather than crashing the attempt.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import dspy
from dspy.predict.predict import Prediction

from clio_agent.gact import context as _ctx
from clio_agent.gact.runtime.type_parsing import (
    VariantSpec,
    _blueprint_module_variant,
)

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef

logger = logging.getLogger(__name__)


@dataclass
class _VariantRunLedger:
    """Per-``BestOfN``/``Refine``-call state shared across its N tries.

    Lives in a contextvar (:data:`_LEDGER`) set once by the run-scoped wrapper's
    ``forward`` and read by BOTH the inner :class:`_RunKeyedModule` (to allocate the
    monotonic per-try ``run_index``) and the compiled reward fn (to record each try's
    score). Survives the ``module.deepcopy()`` DSPy does per try because it is a module
    GLOBAL referenced by name, never a module attribute the deepcopy would clone."""

    next_index: int = 0
    current_index: int = 0
    scores: list[tuple[int, float]] = field(default_factory=list)
    # Per-try real error (run_index, "ExcType: message"). The engine only PRINTS a
    # failed try's exception to stdout and discards it; the inner :class:`_RunKeyedModule`
    # records it here so a TOTAL failure (every try raised) can carry the real root cause
    # into the typed turn-ladder error regardless of N (#953 total-failure normalization).
    errors: list[tuple[int, str]] = field(default_factory=list)


_LEDGER: contextvars.ContextVar[_VariantRunLedger | None] = contextvars.ContextVar(
    "clio_variant_run_ledger", default=None
)


class VariantTotalFailure(RuntimeError):
    """Every try of a declared variant raised (#953): a typed turn-ladder failure.

    The installed ``dspy.BestOfN``/``Refine`` are N-dependent on total failure — their
    ``fail_count`` off-by-one returns ``best_pred=None`` for ``n<=2`` (swallowing the real
    error to a ``None``-ish result the caller mislabels as an empty answer) and raises only
    for ``n>=3``. This wrapper normalizes BOTH into ONE typed failure that ALWAYS surfaces
    the last try's real error + a per-try summary, so the root cause reaches the trace and
    the outcome is identical for n=1, 2, 3. ``str()`` is a clio-owned message (never the raw
    inner error verbatim) so it is not mistaken for a repairable schema-validation miss; the
    structured detail rides ``per_try_errors`` / ``last_error``."""

    def __init__(
        self,
        message: str,
        *,
        agent_id: str,
        variant: str,
        per_try_errors: list[tuple[int, str]],
        last_error: str,
    ) -> None:
        super().__init__(message)
        self.agent_id = agent_id
        self.variant = variant
        self.per_try_errors = per_try_errors
        self.last_error = last_error


def _total_variant_failure(
    agent_id: str,
    variant: str,
    ledger: _VariantRunLedger,
    engine_exc: BaseException | None,
) -> VariantTotalFailure:
    """Build the typed total-failure error from the ledger's per-try record.

    ``engine_exc`` is the exception the engine re-raised on the ``n>=3`` path (the last
    try's real error); on the ``n<=2`` path the engine returned ``None`` and the last
    error is taken from the ledger. Either way the message carries the last error + a
    per-try summary — never N-dependent, never swallowed."""

    per_try = list(ledger.errors)
    if engine_exc is not None:
        last_error = f"{type(engine_exc).__name__}: {engine_exc}"
    elif per_try:
        last_error = per_try[-1][1]
    else:  # pragma: no cover - a None return always has recorded per-try errors
        last_error = "unknown error"
    n = ledger.next_index or len(per_try)
    summary = "; ".join(f"try {idx}: {err}" for idx, err in per_try) or "no per-try detail"
    logger.warning(
        "variant.total_failure agent=%s variant=%s tries=%d last_error=%s",
        agent_id,
        variant,
        n,
        last_error,
    )
    return VariantTotalFailure(
        f"blueprint expert {agent_id!r} variant {variant!r} exhausted all {n} tries "
        f"(every attempt raised); last try error follows | per-try: {summary}",
        agent_id=agent_id,
        variant=variant,
        per_try_errors=per_try,
        last_error=last_error,
    )


class _RunKeyedModule(dspy.Module):
    """Interpose a per-try ``react_run`` discriminator around a variant's inner forward.

    Wraps the declared inner program (``predict``/``chain_of_thought``/``react``) and,
    on each forward, allocates the next ``run_index`` from the active
    :class:`_VariantRunLedger` and sets :func:`clio_agent.gact.context.set_react_run`
    for the duration of the inner call — so the ARC live-plane + transcript-tap KEYS
    partition per try (via ``run_keyed_scope``) while attribution stays on the bare
    agent id. Being a real ``dspy.Module``, it forwards ``get_lm`` / ``set_lm`` /
    ``named_predictors`` through to ``self.inner`` (DSPy recurses into sub-modules), so
    ``dspy.BestOfN``/``Refine`` drive the inner exactly as if unwrapped."""

    def __init__(self, inner: dspy.Module, *, agent_id: str, variant: str) -> None:
        super().__init__()
        self.inner = inner
        self._clio_agent_id = agent_id
        self._clio_variant = variant

    def forward(self, **kwargs: Any) -> Any:
        ledger = _LEDGER.get()
        run_index = ledger.next_index if ledger is not None else 0
        if ledger is not None:
            ledger.current_index = run_index
            ledger.next_index += 1
        token = _ctx.set_react_run(run_index)
        logger.info(
            "variant.try agent=%s variant=%s run_index=%d",
            self._clio_agent_id,
            self._clio_variant,
            run_index,
        )
        try:
            return self.inner(**kwargs)
        except Exception as exc:  # noqa: BLE001 - record the REAL error the engine only prints
            # The engine (`dspy.BestOfN`/`Refine`) catches + PRINTS each failed try and
            # discards the exception; capture it on the ledger so a total failure can carry
            # the real root cause into the typed turn-ladder error (#953). Re-raise so the
            # engine's own selection loop is unchanged.
            if ledger is not None:
                ledger.errors.append((run_index, f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            _ctx.reset(token)


class _RunScopedVariantMixin:
    """Bracket a ``dspy.BestOfN``/``Refine`` ``forward`` with the per-call run ledger.

    Installs a fresh :class:`_VariantRunLedger` for the whole variant call (visible to
    the inner :class:`_RunKeyedModule` and the reward fn, both of which run
    synchronously within it), delegates to the REAL engine ``forward`` via ``super()``,
    then stamps the winning try's index + score onto the returned prediction. MRO places
    this mixin before the engine class, so ``isinstance(x, dspy.BestOfN)`` /
    ``dspy.Refine`` still holds and the engine's selection loop is used verbatim."""

    _clio_variant: str = ""
    _clio_agent_id: str = ""

    def forward(self, **kwargs: Any) -> Any:
        ledger = _VariantRunLedger()
        token = _LEDGER.set(ledger)
        try:
            try:
                pred = super().forward(**kwargs)  # type: ignore[misc]
            except Exception as engine_exc:  # noqa: BLE001
                # dspy raises the last try's exception on TOTAL failure only for n>=3
                # (fail_count off-by-one). Normalize to the typed total-failure so the
                # outcome is identical for every N and the root cause reaches the trace.
                raise _total_variant_failure(
                    self._clio_agent_id, self._clio_variant, ledger, engine_exc
                ) from engine_exc
        finally:
            _LEDGER.reset(token)
        if pred is None:
            # n<=2 total failure: dspy returns best_pred=None (every try raised, none
            # selected). ALWAYS a typed turn-ladder failure — never swallowed to None.
            raise _total_variant_failure(self._clio_agent_id, self._clio_variant, ledger, None)
        _stamp_variant_selection(pred, self._clio_variant, self._clio_agent_id, ledger)
        return pred


class _RunScopedBestOfN(_RunScopedVariantMixin, dspy.BestOfN):
    """A real ``dspy.BestOfN`` (subclass) that run-keys each try + stamps the winner."""


class _RunScopedRefine(_RunScopedVariantMixin, dspy.Refine):
    """A real ``dspy.Refine`` (subclass) that run-keys each try + stamps the winner."""


def _stamp_variant_selection(
    pred: Any,
    variant: str,
    agent_id: str,
    ledger: _VariantRunLedger,
) -> None:
    """Record the winning try (typed, additive) on the returned prediction + a log.

    The winner is the highest-reward try (Python ``max`` returns the FIRST maximal
    element, matching the engine's strict ``reward > best_reward`` replacement and its
    ``reward >= threshold`` early break, over the tries the ledger observed)."""
    if pred is None or not ledger.scores:
        return
    winning_index, winning_score = max(ledger.scores, key=lambda pair: pair[1])
    selection = {
        "variant": variant,
        "n": ledger.next_index,
        "winning_index": winning_index,
        "winning_score": winning_score,
        "scores": [{"run_index": idx, "score": score} for idx, score in ledger.scores],
    }
    try:
        pred.variant_selection = selection
    except Exception:  # noqa: BLE001 - never let observability metadata break a turn
        logger.warning("variant.selection.stamp_failed agent=%s variant=%s", agent_id, variant)
    logger.info(
        "variant.selected agent=%s variant=%s winning_index=%d winning_score=%.3f tries=%d",
        agent_id,
        variant,
        winning_index,
        winning_score,
        ledger.next_index,
    )


def _clamp_score(raw: Any) -> float:
    """Parse an LM-judge score to a float in ``[0.0, 1.0]``; raise on non-numeric.

    The caller (the compiled reward fn) catches the raise and scores ``0.0`` with a
    typed log — a parse failure is a bounded degradation, never a crash of the try."""
    value = float(raw)
    if value != value:  # NaN
        raise ValueError("reward score is NaN")
    return max(0.0, min(1.0, value))


def _build_reward_signature(spec: VariantSpec) -> type[dspy.Signature]:
    """Compile the declared reward mapping into a real ``dspy.Signature`` (LM judge).

    Input fields = the declared module inputs (from the call kwargs) plus the scored
    target field (from the prediction); one ``score: float`` output; the declaration's
    ``instructions`` become the signature docstring/rubric."""
    from dspy.signatures import InputField, OutputField  # noqa: PLC0415

    fields: dict[str, Any] = {}
    for name in spec.reward_inputs:
        desc = spec.reward_input_descs.get(name) or f"The {name} given to the agent."
        fields[name] = (str, InputField(desc=desc))
    fields[spec.reward_target] = (
        str,
        InputField(desc=f"The agent's {spec.reward_target} to be scored."),
    )
    fields["score"] = (
        float,
        OutputField(desc="A single score in [0.0, 1.0]; higher is better."),
    )
    return dspy.Signature(fields, spec.reward_instructions)


def compile_reward_fn(spec: VariantSpec, *, agent_id: str) -> Callable[[dict, Prediction], float]:
    """Compile the declared reward signature into a source-backed real reward ``def``.

    Returns a nested ``def scored_reward(kwargs, pred) -> float`` defined in THIS module
    — so ``inspect.getsource`` (which ``dspy.Refine`` calls on the reward fn) resolves
    against this file's on-disk source. The function scores each attempt with an
    LM-as-judge ``dspy.Predict`` over the compiled reward signature (using the ambient
    ``dspy.settings.lm`` the expert forward configures), clamps/parses defensively, and
    records the score on the active ledger. It NEVER raises: a parse failure scores
    ``0.0`` with a typed ``variant.reward.parse_failed`` log (no silent degrade)."""
    signature = _build_reward_signature(spec)
    reward_inputs = spec.reward_inputs
    target_field = spec.reward_target

    def scored_reward(kwargs: dict, pred: Prediction) -> float:
        """Generated LM-as-judge reward fn (source-backed for dspy.Refine.getsource)."""
        ledger = _LEDGER.get()
        run_index = ledger.current_index if ledger is not None else 0
        try:
            judge_inputs: dict[str, str] = {
                name: str(kwargs.get(name, "") or "") for name in reward_inputs
            }
            judge_inputs[target_field] = str(getattr(pred, target_field, "") or "")
            judged = dspy.Predict(signature)(**judge_inputs)
            score = _clamp_score(getattr(judged, "score", None))
        except Exception as exc:  # noqa: BLE001 - typed 0.0 degrade, never a crash
            logger.warning(
                "variant.reward.parse_failed agent=%s run_index=%d error=%s",
                agent_id,
                run_index,
                exc,
            )
            score = 0.0
        if ledger is not None:
            ledger.scores.append((run_index, score))
        logger.info("variant.reward agent=%s run_index=%d score=%.3f", agent_id, run_index, score)
        return score

    return scored_reward


def wrap_module_variant(inner: dspy.Module, agent_def: "AgentDef") -> dspy.Module:
    """Wrap ``inner`` in the declared ``dspy.BestOfN``/``Refine`` variant, or return it.

    Called from the ``builders.py`` module-construction dispatch AFTER the inner
    ``predict``/``chain_of_thought``/``react`` program is built. Returns ``inner``
    unchanged when no ``module.variant`` is declared; otherwise returns a REAL
    (subclassed) ``dspy.BestOfN``/``Refine`` whose ``module`` is the run-keyed inner and
    whose ``reward_fn`` is the compiled source-backed judge. ``fail_count`` is left at
    the engine default (``= N``). Raises ``ValueError`` on an invalid declaration (the
    same typed error the loader surfaces)."""
    spec = _blueprint_module_variant(agent_def)
    if spec is None:
        return inner
    agent_id = str(getattr(agent_def, "id", "") or "")
    reward_fn = compile_reward_fn(spec, agent_id=agent_id)
    keyed = _RunKeyedModule(inner, agent_id=agent_id, variant=spec.variant)
    cls = _RunScopedBestOfN if spec.variant == "best_of_n" else _RunScopedRefine
    wrapped = cls(
        module=keyed,
        N=spec.n,
        reward_fn=reward_fn,
        threshold=spec.threshold,
    )
    wrapped._clio_variant = spec.variant
    wrapped._clio_agent_id = agent_id
    return wrapped
