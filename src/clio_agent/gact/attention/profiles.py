"""Adapter from verified connector rows to shared, versioned attention semantics."""

from __future__ import annotations

from clio_schemas.attention import (
    AttentionProfile,
    AttentionReduction,
    SparseAttentionRow,
    reduce_attention,
)

from clio_agent.gact.attention.contract import AttentionStep
from clio_agent.gact.attention.reasons import AttentionUnavailable

#: The SPOTTER attention semantics (spotter-ai-genesis/experiments
#: ``bench/opal/analytics/attn_analytics.py``): for prefill token ``j`` an item
#: scores ``sum_i 0.5**i * val_all_max[step_i][j]`` over the item's decode steps,
#: ``i`` counted from its first step, weights not normalized. Lines and blocks
#: take the max of their tokens; the display scales each item to its own max.
ATTN_ANALYTICS = AttentionProfile(
    name="attn-analytics",
    metric="max",
    weighting="exponential",
    decay_base=0.5,
    direction="forward",
    weight_normalization="none",
    block_reduction="max",
    display_scaling="max",
)
DEFAULT_PROFILE = ATTN_ANALYTICS

#: The connector's fill value for a row slot no position occupies
#: (vllm-attn-connector ``NO_ENTRY``): a segment with fewer candidates than its
#: keep count, e.g. the first segment, whose sink position 0 is never selected.
NO_ENTRY = -1


def _row(step: AttentionStep) -> SparseAttentionRow:
    kept = [k for k in range(len(step.pos)) if int(step.pos[k]) != NO_ENTRY]
    return SparseAttentionRow(
        step=step.step,
        positions=tuple(int(step.pos[k]) for k in kept),
        mean=tuple(float(step.mean[k]) for k in kept),
        peak=tuple(float(step.max[k]) for k in kept),
        residual=step.residual,
    )


def reduce_steps(
    steps: list[AttentionStep], total: int, profile: AttentionProfile
) -> AttentionReduction:
    """Reduce one request's captured rows using the canonical reviewer implementation.

    Unused ``NO_ENTRY`` slots carry no position and value 0, so they are dropped
    here; any other position outside the prompt is still refused as malformed.
    """
    try:
        return reduce_attention((_row(step) for step in steps), total, profile)
    except ValueError as exc:
        raise AttentionUnavailable("attention_record_malformed", str(exc)) from exc
