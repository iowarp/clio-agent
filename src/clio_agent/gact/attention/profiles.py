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


def reduce_steps(
    steps: list[AttentionStep], total: int, profile: AttentionProfile
) -> AttentionReduction:
    """Reduce one request's captured rows using the canonical reviewer implementation."""
    try:
        return reduce_attention(
            (
                SparseAttentionRow(
                    step=step.step,
                    positions=tuple(int(value) for value in step.pos),
                    mean=tuple(float(value) for value in step.mean),
                    peak=tuple(float(value) for value in step.max),
                    residual=step.residual,
                )
                for step in steps
            ),
            total,
            profile,
        )
    except ValueError as exc:
        raise AttentionUnavailable("attention_record_malformed", str(exc)) from exc
