"""Context-sizing strategies: a registry of pure ``(profile, budget) -> (context, reason)``.

"Fit to GPU" is the one context choice that needs the hardware, and how to fit
is a policy, not a fact. Every policy is a :class:`ContextStrategy` in one
registry:

* :data:`FIT_TO_GPU` (``fit_to_gpu``, the default): the largest context up to
  the model's own maximum whose KV cache fits the deployment's GPU budget
  beside the weights, with headroom;
* research strategies register with :func:`register_strategy` (or the
  ``clio_agent.context_strategies`` entry-point group, loaded on first use).

Which strategy a deployment uses by default is the ``lm.context_sizing_strategy``
setting (:func:`configured_strategy_id`); a deployment may name another.

A strategy is only called with its inputs known (:func:`fit_unavailable_reason`
says what is missing otherwise). :func:`default_context` is the decision a
deployment takes when the person chose nothing: the strategy when it can run,
else the model's own maximum capped at :data:`UNKNOWN_BUDGET_CAP` -- without a
GPU budget the KV cache may land in system RAM, where a long trained context
costs tens of GiB.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from clio_agent.context_sizing.profile import GpuBudget, ModelMemoryProfile

logger = logging.getLogger(__name__)

#: Contexts a strategy picks are floored to a multiple of this many tokens.
CONTEXT_GRANULE = 4096
#: The ceiling used when no GPU budget is known.
UNKNOWN_BUDGET_CAP = 32768
#: The entry-point group research strategies may register through.
ENTRY_POINT_GROUP = "clio_agent.context_strategies"
#: The built-in default strategy id.
DEFAULT_STRATEGY = "fit_to_gpu"


@dataclass(frozen=True)
class SizedContext:
    """A context length and the sentence that explains it."""

    tokens: int
    reason: str


StrategyFn = Callable[[ModelMemoryProfile, GpuBudget], SizedContext]


@dataclass(frozen=True)
class ContextStrategy:
    """One registered way to size a context to the GPU.

    Attributes:
        id: The stable id a setting or deployment names.
        label: A short name for a selector.
        description: One sentence on what it optimises for.
        size: The pure sizing function; called only with a known layout,
            trained context and budget.
    """

    id: str
    label: str
    description: str
    size: StrategyFn


def largest_fitting(profile: ModelMemoryProfile, limit_bytes: int, ceiling: int) -> int:
    """The largest multiple of :data:`CONTEXT_GRANULE` up to ``ceiling`` within ``limit_bytes``.

    The KV cost is monotone in the context (sliding-window layers stop
    growing at their window), so a binary search over granules is exact.
    Returns 0 when not even one granule fits.
    """

    low, high = 0, ceiling // CONTEXT_GRANULE
    while low < high:
        middle = (low + high + 1) // 2
        cost = profile.kv_bytes(middle * CONTEXT_GRANULE)
        if cost is not None and cost <= limit_bytes:
            low = middle
        else:
            high = middle - 1
    return low * CONTEXT_GRANULE


def fit_to_gpu(profile: ModelMemoryProfile, budget: GpuBudget) -> SizedContext:
    """The model's own maximum, capped so its KV cache fits ``budget``."""

    trained = int(profile.trained_context or 0)
    limit = budget.per_sequence_bytes
    full = profile.kv_bytes(trained)
    if full is not None and full <= limit:
        return SizedContext(trained, f"model's trained context {trained}")
    tokens = max(largest_fitting(profile, limit, trained), CONTEXT_GRANULE)
    if tokens >= trained:
        return SizedContext(trained, f"model's trained context {trained}")
    return SizedContext(
        tokens,
        f"trained context {trained} capped to {tokens} so the KV cache fits "
        f"{budget.describe()} beside the weights",
    )


FIT_TO_GPU = ContextStrategy(
    id=DEFAULT_STRATEGY,
    label="Fit to GPU",
    description=(
        "The largest context up to the model's own maximum whose KV cache fits the "
        "deployment's GPU memory beside the weights, with headroom."
    ),
    size=fit_to_gpu,
)

_REGISTRY: dict[str, ContextStrategy] = {FIT_TO_GPU.id: FIT_TO_GPU}
_ENTRY_POINTS_LOADED = False


def register_strategy(strategy: ContextStrategy, *, replace: bool = False) -> ContextStrategy:
    """Add ``strategy`` to the registry (a second one under the same id is refused).

    Raises:
        ValueError: When the id is empty or taken and ``replace`` is false.
    """

    if not strategy.id.strip():
        raise ValueError("A context strategy needs an id")
    if strategy.id in _REGISTRY and not replace:
        raise ValueError(f"Context strategy {strategy.id!r} is already registered")
    _REGISTRY[strategy.id] = strategy
    return strategy


def unregister_strategy(strategy_id: str) -> None:
    """Remove a registered strategy (the built-in default cannot be removed)."""

    if strategy_id == DEFAULT_STRATEGY:
        raise ValueError("The default context strategy cannot be removed")
    _REGISTRY.pop(strategy_id, None)


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = point.load()
            # The point names a ContextStrategy, or a factory returning one.
            strategy = loaded if isinstance(loaded, ContextStrategy) else loaded()
            if isinstance(strategy, ContextStrategy) and strategy.id not in _REGISTRY:
                _REGISTRY[strategy.id] = strategy
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            # A broken plugin must not break sizing: it is left out, and said so.
            logger.warning(
                "context strategy entry point not loaded: name=%s reason=%s", point.name, exc
            )


def strategies() -> list[ContextStrategy]:
    """Every registered strategy, the default first."""

    _load_entry_points()
    rest = sorted((s for s in _REGISTRY.values() if s.id != DEFAULT_STRATEGY), key=lambda s: s.id)
    return [_REGISTRY[DEFAULT_STRATEGY], *rest]


def get_strategy(strategy_id: str) -> ContextStrategy:
    """The registered strategy ``strategy_id``.

    Raises:
        ValueError: Naming the registered ids when it is unknown.
    """

    _load_entry_points()
    found = _REGISTRY.get(strategy_id)
    if found is None:
        known = ", ".join(s.id for s in strategies())
        raise ValueError(f"Unknown context sizing strategy {strategy_id!r} (registered: {known})")
    return found


def configured_strategy_id() -> str:
    """The default strategy from ``lm.context_sizing_strategy`` (``fit_to_gpu``)."""

    from clio_agent import conf  # noqa: PLC0415 - keep this module import-light

    value = conf.resolve(
        "lm.context_sizing_strategy",
        env="CLIO_LM_CONTEXT_SIZING_STRATEGY",
        default="fit_to_gpu",
        cast=conf.as_str,
    )
    return str(value or DEFAULT_STRATEGY).strip() or DEFAULT_STRATEGY


def fit_unavailable_reason(profile: ModelMemoryProfile | None, budget: GpuBudget | None) -> str:
    """Why no GPU-fitting strategy can run, or ``""`` when every input is known."""

    if profile is None:
        return "the model's layout could not be read"
    if not profile.trained_context:
        return "the model does not state its maximum context"
    if not profile.layout_known:
        return "the model's attention layout (layers, KV heads, head size) is not known"
    if budget is None:
        return "no GPU memory budget is known to size the KV cache against"
    return ""


def size_with(
    strategy_id: str, profile: ModelMemoryProfile | None, budget: GpuBudget | None
) -> SizedContext:
    """Run ``strategy_id``; its result is clamped to ``[1, trained]``.

    Raises:
        ValueError: When the strategy is unknown or an input is missing.
    """

    strategy = get_strategy(strategy_id)
    missing = fit_unavailable_reason(profile, budget)
    if missing:
        raise ValueError(f"Fit to GPU cannot be computed: {missing}")
    assert profile is not None and budget is not None and profile.trained_context
    sized = strategy.size(profile, budget)
    tokens = min(max(int(sized.tokens), 1), int(profile.trained_context))
    return SizedContext(tokens, sized.reason)


def default_context(
    profile: ModelMemoryProfile | None, budget: GpuBudget | None, strategy_id: str
) -> SizedContext | None:
    """The context to serve when the person chose none.

    The strategy when its inputs are known; else the model's own maximum,
    capped at :data:`UNKNOWN_BUDGET_CAP`. None when the model does not state
    its maximum (the engine's own default then stays in force).
    """

    if profile is None or not profile.trained_context:
        return None
    missing = fit_unavailable_reason(profile, budget)
    if not missing:
        return size_with(strategy_id, profile, budget)
    trained = profile.trained_context
    if trained <= UNKNOWN_BUDGET_CAP:
        return SizedContext(trained, f"model's trained context {trained}")
    return SizedContext(
        UNKNOWN_BUDGET_CAP, f"trained context {trained} capped to {UNKNOWN_BUDGET_CAP}: {missing}"
    )


__all__ = [
    "CONTEXT_GRANULE",
    "DEFAULT_STRATEGY",
    "ENTRY_POINT_GROUP",
    "FIT_TO_GPU",
    "UNKNOWN_BUDGET_CAP",
    "ContextStrategy",
    "SizedContext",
    "StrategyFn",
    "configured_strategy_id",
    "default_context",
    "fit_to_gpu",
    "fit_unavailable_reason",
    "get_strategy",
    "largest_fitting",
    "register_strategy",
    "size_with",
    "strategies",
    "unregister_strategy",
]
