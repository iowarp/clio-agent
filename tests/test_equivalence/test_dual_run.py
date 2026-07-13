"""Live dual-run A/B scaffold acceptance (design §4.1.B, acceptance (i) SSE/context/trace).

Drives the SAME scripted inputs through the real loop twice and diffs the four
surfaces. For S0 the two legs share a config (the DETERMINISM baseline), which is how
the plan's open question is answered empirically: after masking ONLY ids + wall-clock
(SSE/persistence) and NOTHING (context/trace), the diff is EMPTY — so a clean masked
diff IS a valid equivalence signal, because ordering, content, and event-type
presence are all unmasked.

Exercises the exotic-tool-output path (caveat a) in every run and the injected
tool-crash path (caveat b) in ``test_dual_run_crash_path_equivalent``.
"""

from __future__ import annotations

import pytest

from tests.equivalence import dual_run as DR


@pytest.fixture
def _two_tmp(tmp_path):
    a = tmp_path / "runA"
    b = tmp_path / "runB"
    a.mkdir()
    b.mkdir()
    return a, b


def test_dual_run_all_four_surfaces_empty(_two_tmp, capsys) -> None:
    """The determinism baseline: identical configs → EMPTY diff on all four surfaces
    (SSE, persistence, context, trace) after the declared masking."""

    a, b = _two_tmp
    cfg = DR.WriterConfig(label="baseline")
    report = DR.dual_run(cfg, cfg, a, b)

    # Emit the verdict so the run's masking is auditable in the test log.
    with capsys.disabled():
        print("\n=== DUAL-RUN DETERMINISM VERDICT ===\n" + report.pretty())

    for name, rep in report.reports.items():
        assert rep.empty, f"surface {name!r} diverged:\n{rep.pretty()}"
    assert report.all_empty


def test_dual_run_crash_path_equivalent(_two_tmp) -> None:
    """Caveat b: two runs with an injected tool crash still agree on SSE + persistence.

    The crash exercises the error envelope; equivalence between two crash runs proves
    the error path is deterministic under the harness's masking (a divergence here
    would flag a non-deterministic failure surface)."""

    a, b = _two_tmp
    cfg = DR.WriterConfig(label="crash")
    report = DR.dual_run(cfg, cfg, a, b, crash=True)
    # SSE + persistence are the surfaces the gact turn (which crashed) produces.
    assert report.reports["sse"].empty, report.reports["sse"].pretty()
    assert report.reports["persistence"].empty, report.reports["persistence"].pretty()


def test_dual_run_detects_a_surface_divergence(_two_tmp, monkeypatch) -> None:
    """Negative control: if one leg's SSE stream is perturbed, the dual-run reports it.

    Sabotages the SSE capture of the SECOND leg (drops a served event type) and
    asserts the scaffold's SSE diff goes RED — proving the A/B is a real gate, not a
    pass-through."""

    a, b = _two_tmp
    cfg = DR.WriterConfig(label="sabotage")
    cap_a = DR.capture_surfaces(cfg, a)
    cap_b = DR.capture_surfaces(cfg, b)
    # Suppress every 'turn.started' event in leg B's SSE — a deliberate drop.
    cap_b.sse = [ev for ev in cap_b.sse if getattr(ev, "type", "") != "turn.started"]

    # Re-diff SSE directly (mirrors dual_run's SSE branch) and assert it fires.
    import tests.equivalence.normalizers as N

    if "turn.started" in N.sse_present_types(cap_a.sse):
        report = N.diff_sse(cap_a.sse, cap_b.sse)
        assert not report.empty
        assert report.divergence.reason == "drop_detection"
    else:
        pytest.skip("turn.started not present in this build's SSE stream")
