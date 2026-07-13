"""``reload == live`` on real fixtures — the #737 S5 acceptance proof (design (d)).

The decisive persistence-by-reference proof: for every ledger in the sweep corpus,
minting the ``message_part`` atoms from the messages-store rows and then ASSEMBLING the
transcript back from those atoms reproduces the original ledger BYTE-EQUAL under the
§4.1.A persistence normalizer (``Message(**payload).to_wire()``). Because both the SSE
spine and ``GET /messages`` project the SAME atoms, an empty diff here is exactly the
``reload == live`` invariant (design §2.8c / §6.3): the reload path (assemble-by-reference)
equals the captured live content.

These 60-odd ledgers PRE-DATE the atom family, so the proof runs through the documented
migration seam :func:`~clio_agent.gact.transcript_projection.mint_atoms_from_ledger`
(typed failure per un-mintable message — no silent skips). Runs on BOTH ARC backends
(acceptance iv): LocalFS always, and the clio-core daemon when its binding is present.

Default corpus = the committed REDACTED stand-in; point ``$CLIO_EQUIV_CORPUS`` at the
local ~60-ledger dir for the full-fidelity sweep. The test prints the per-fixture pass
count so the sweep numbers are on the record.

SABOTAGE (recorded, run manually, NOT committed as a second test):

* (a) drop one wire field in the mint — delete e.g. the ``"cost_usd"`` key from the
  ``envelope`` in ``part_atoms._atom_content``: this sweep goes RED with a precise
  ``DIVERGENCE at .cost_usd (keys)`` on the FIRST fixture carrying that field — the
  field-path differ names the exact dropped path, proving the gate is not a tautology.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.transcript_projection import (
    assemble_session_messages,
    mint_atoms_from_ledger,
)
from clio_agent.gact.types import Message
from tests.equivalence import corpus as C
from tests.equivalence import normalizers as N


@pytest.fixture(params=["local", "cte"])
def arc(request: Any, tmp_path) -> Iterator[ARCMemory]:
    """A fresh ARCMemory on BOTH backends (the ``cte`` leg skips without the binding)."""

    backend = request.param
    if backend == "cte":
        pytest.importorskip("clio_cte_core_ext")
        from clio_agent.arc.storage import make_arc_store

        memory = ARCMemory(store=make_arc_store(backend="cte"))
        memory.clear_all()
        try:
            yield memory
        finally:
            memory.clear_all()
        return
    yield ARCMemory(data_dir=str(tmp_path / "arc"))


def test_reload_equals_live_over_corpus(arc: ARCMemory) -> None:
    """Mint atoms from each ledger, assemble by reference, diff EMPTY under §4.1.A.

    The full ``reload == live`` sweep: assemble(mint(ledger)) == ledger, byte-equal.
    Prints ``passed N / N`` so the corpus numbers are recorded; a single field
    divergence fails with the precise field path.
    """

    ledgers = C.sweep_corpus()
    assert ledgers, "sweep corpus is empty (no committed corpus and no CLIO_EQUIV_CORPUS)"

    passed = 0
    divergences: list[str] = []
    for sid, rows in ledgers:
        # A brand-new session id per ledger keeps atom lanes isolated on the shared log.
        session_id = f"reloadlive_{sid}"
        messages = [Message(**payload) for payload in rows]
        mint_atoms_from_ledger(arc, session_id, messages)
        assembled = assemble_session_messages(arc, session_id)
        report = N.diff_persistence(
            rows, [m.model_dump(exclude_none=True) for m in assembled]
        )
        if report.empty:
            passed += 1
        else:
            divergences.append(f"{sid}: {report.pretty()}")

    print(f"\nreload==live corpus sweep: passed {passed} / {len(ledgers)}")
    assert not divergences, "reload != live on:\n" + "\n".join(divergences)
    assert passed == len(ledgers)


def test_reload_equals_live_preserves_message_order_and_count(arc: ARCMemory) -> None:
    """Assembly reproduces the exact message COUNT and ORDER (ids in sequence).

    A per-fixture structural pin distinct from the byte diff: assembly must not drop,
    duplicate, or reorder messages — the chronological id sequence is identical.
    """

    ledgers = C.sweep_corpus()
    checked = 0
    for sid, rows in ledgers:
        if not rows:
            continue
        session_id = f"order_{sid}"
        messages = [Message(**payload) for payload in rows]
        mint_atoms_from_ledger(arc, session_id, messages)
        assembled = assemble_session_messages(arc, session_id)
        assert [m.id for m in assembled] == [m.id for m in messages], sid
        checked += 1
    if checked == 0:
        pytest.skip("no non-empty ledger in the corpus")
