"""Reduce sparse per-step attention rows to per-token and per-section mass.

Rows are sparse: the connector keeps the top ``top_pct`` positions per segment
plus one ``residual`` (the mean mass it did not retain). Everything here keeps
that explicit -- an unretained position is "not in the top N%", never "zero",
and the residual is reported next to every share instead of being spread out.

The per-token value is the **mean** aggregation (``val_all_avg``), averaged
over the selected steps: it is a true distribution, so section shares plus the
residual sum to 1. The **max** aggregation is kept per token as the peak, which
is what the connector selected on and what locates sharp retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from clio_agent.gact.attention.contract import AttentionStep, AttentionSummary
from clio_agent.gact.attention.ranges import GAP_LABEL, DeclaredRange


@dataclass(frozen=True)
class Section:
    """A contiguous prompt token span with its transcript-section labels."""

    lo: int
    hi: int
    domain: str
    label: str
    message_index: int | None
    char_lo: int | None
    char_hi: int | None


@dataclass(frozen=True)
class Mass:
    """Per-prompt-token attention for a set of steps (length ``T`` each)."""

    mean: np.ndarray
    peak: np.ndarray
    residual: float
    steps: int
    exact_totals: bool


def sections_with_gaps(ranges: list[DeclaredRange], total: int) -> list[Section]:
    """Declared ranges plus the gaps between them: a complete partition of ``[0, T)``.

    Gaps are chat-template markers and field headers; they carry the e2e
    vocabulary's ``"prose gap"`` label under the ``template`` domain.
    """
    out: list[Section] = []
    cursor = 0
    for r in sorted(ranges, key=lambda r: r.lo):
        lo, hi = max(r.lo, cursor), min(r.hi, total)
        if hi <= lo:
            continue
        if lo > cursor:
            out.append(Section(cursor, lo, "template", GAP_LABEL, None, None, None))
        out.append(Section(lo, hi, r.domain, r.label, r.message_index, r.char_lo, r.char_hi))
        cursor = hi
    if cursor < total:
        out.append(Section(cursor, total, "template", GAP_LABEL, None, None, None))
    return out


def declared_ranges_match(ranges: list[DeclaredRange], summary: AttentionSummary) -> bool:
    """Every declared ``(lo, hi)`` is a captured segment, verbatim (the e2e rule)."""
    captured = set(summary.segments)
    return all((r.lo, r.hi) in captured for r in ranges)


def mass_from_steps(steps: list[AttentionStep], total: int) -> Mass:
    """Average the selected steps' retained mean mass per prompt token."""
    mean = np.zeros(total, dtype=np.float64)
    peak = np.zeros(total, dtype=np.float64)
    residual = 0.0
    for step in steps:
        keep = (step.pos >= 0) & (step.pos < total)
        pos = step.pos[keep].astype(np.int64)
        np.add.at(mean, pos, step.mean[keep].astype(np.float64))
        np.maximum.at(peak, pos, step.max[keep].astype(np.float64))
        residual += step.residual
    n = max(1, len(steps))
    return Mass(
        mean=mean / n, peak=peak, residual=residual / n, steps=len(steps), exact_totals=False
    )


def mass_from_summary(summary: AttentionSummary) -> Mass | None:
    """Whole-response totals from ``attn_sum`` (every position, nothing dropped)."""
    if summary.attn_sum is None or summary.decode_steps <= 0:
        return None
    mean = summary.attn_sum.astype(np.float64) / summary.decode_steps
    return Mass(
        mean=mean,
        peak=np.zeros_like(mean),
        residual=0.0,
        steps=summary.decode_steps,
        exact_totals=True,
    )


def section_shares(sections: list[Section], mass: Mass) -> list[float]:
    """Mass per section (a fraction of one step's attention, averaged over steps)."""
    csum = np.concatenate([[0.0], np.cumsum(mass.mean)])
    return [float(csum[s.hi] - csum[s.lo]) for s in sections]


def section_peaks(sections: list[Section], mass: Mass) -> list[float]:
    """Max ``val_all_max`` inside each section (0 when nothing retained there)."""
    return [float(mass.peak[s.lo : s.hi].max()) if s.hi > s.lo else 0.0 for s in sections]


def domain_shares(sections: list[Section], shares: list[float]) -> dict[str, float]:
    """Section shares summed per domain, for the Sources tab."""
    out: dict[str, float] = {}
    for section, share in zip(sections, shares, strict=True):
        out[section.domain] = out.get(section.domain, 0.0) + share
    return out
