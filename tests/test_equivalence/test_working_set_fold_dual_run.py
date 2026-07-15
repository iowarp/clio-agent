"""S2 write-path proof: the working-set FOLD is equivalent to the parallel write.

The #737 S2 slice replaces the parallel working-set write with a fold of the
canonical ``_events`` log. That is a WRITE-PATH change, so the captured corpus (built
by the OLD writer) cannot validate it (design C2 / §4.1.B) — it needs a **live
dual-run A/B**: the SAME scripted turn inputs driven once with ``working_set_fold``
OFF (the old parallel write) and once ON (the fold), with the four frozen surfaces
diffed under the §4.1.A normalization. The scripted inputs include the exotic tool
output (caveat a) and an injected crash (caveat b) — exactly the paths a fold
silently diverges on.

A clean four-surface diff here IS the equivalence signal: context + trace are
compared byte-for-byte (no masking), so identical ``(kind, content, order, status)``
proves the fold reproduces the working set the old writer materialized.
"""

from __future__ import annotations

from tests.equivalence.dual_run import WriterConfig, dual_run


def test_fold_matches_parallel_write_all_surfaces(tmp_path) -> None:
    """OFF (parallel write) vs ON (fold) agree on all four surfaces, including the
    exotic tool output the happy-path captures never exercise (caveat a).

    v0.8.0: the classic loop was deleted, so the dual run drives the V2 loop only
    (the fold on/off axis is what this proof is about, not the loop)."""
    off = WriterConfig(label="fold_off", working_set_fold=False)
    on = WriterConfig(label="fold_on", working_set_fold=True)
    report = dual_run(off, on, tmp_path / "off", tmp_path / "on")
    assert report.all_empty, "fold diverged from the parallel write:\n" + report.pretty()


def test_fold_matches_parallel_write_crash_path(tmp_path) -> None:
    """OFF vs ON agree on the SSE + persistence error envelopes when a turn crashes
    mid-flight (caveat b)."""
    off = WriterConfig(label="fold_off", working_set_fold=False)
    on = WriterConfig(label="fold_on", working_set_fold=True)
    report = dual_run(off, on, tmp_path / "off", tmp_path / "on", crash=True)
    assert report.reports["sse"].empty, "SSE diverged on crash:\n" + report.reports["sse"].pretty()
    assert report.reports["persistence"].empty, (
        "persistence diverged on crash:\n" + report.reports["persistence"].pretty()
    )
