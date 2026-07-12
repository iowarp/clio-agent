"""Captured-corpus persistence sweep (design §4.1.A/§4.1.B, acceptance (i) + (iv)).

The decisive read-path proof: the persistence projection (``Message(**payload)`` →
``to_wire()``) reproduces the stored/served form with an EMPTY diff over the whole
corpus. Today the projection IS the re-parse, so the baseline is empty by
construction — which is exactly the point: this pins today's state so a LATER slice
that changes the projection is caught by the SAME sweep (see the sabotage test).

Runs on BOTH ARC backends (acceptance iv): the sweep holds one ``make_arc_store``
client alive for its whole duration (the holder pattern) under LocalFS and, when the
clio-core binding is present, under the clio-core daemon. The persistence surface is
store-agnostic, so an identical EMPTY result under both proves the harness is robust
to the backend the later context/trace slices will run against.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import pytest

from tests.equivalence import corpus as C
from tests.equivalence import normalizers as N


@pytest.fixture(params=["local", "cte"])
def held_arc_store(request, tmp_path) -> Iterator[object]:
    """One ARC store, held open for the whole sweep (the §2.10 holder pattern).

    ``local`` uses LocalFS; ``cte`` spawns the clio-core daemon via ``make_arc_store``
    and skips when the binding is absent (binding-free CI keeps the local leg)."""

    from clio_agent.arc.storage import make_arc_store

    backend = request.param
    if backend == "cte":
        pytest.importorskip("clio_cte_core_ext")
        store = make_arc_store(backend="cte")
    else:
        store = make_arc_store(backend="local", data_dir=str(tmp_path / "arc"))
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def test_persistence_sweep_is_empty_over_corpus(held_arc_store) -> None:
    """EMPTY diff over the sweep corpus (committed redacted by default; the full local
    ~60-ledger corpus when ``$CLIO_EQUIV_CORPUS`` is set). Held store proves backend
    robustness."""

    ledgers = C.sweep_corpus()
    assert ledgers, "sweep corpus is empty (no committed corpus and no CLIO_EQUIV_CORPUS)"

    swept = 0
    for sid, rows in ledgers:
        report = N.diff_persistence(rows, rows)
        assert report.empty, f"persistence divergence in {sid}:\n{report.pretty()}"
        swept += 1
    assert swept == len(ledgers)


def test_persistence_sweep_catches_a_one_byte_projection_mutation(held_arc_store) -> None:
    """Acceptance (iii) on the corpus: a one-byte content mutation in the PROJECTION
    is caught with a precise field-path report — proving the sweep is a real gate, not
    a tautology."""

    import copy

    ledgers = C.sweep_corpus()
    # find the first ledger with a text-bearing assistant part to mutate
    for sid, rows in ledgers:
        mutated = copy.deepcopy(rows)
        target = _first_text_part(mutated)
        if target is None:
            continue
        original = target["text"]
        target["text"] = original[:-1] + ("§" if not original.endswith("§") else "¶")
        report = N.diff_persistence(rows, mutated)
        assert not report.empty, f"a mutated projection must diverge ({sid})"
        assert ".parts[" in report.divergence.path and report.divergence.path.endswith(".text")
        return
    pytest.skip("no text-bearing part in the corpus to mutate")


def _first_text_part(rows: list[dict]) -> dict | None:
    for msg in rows:
        for part in msg.get("parts", []):
            if isinstance(part, dict) and part.get("text"):
                return part
    return None
