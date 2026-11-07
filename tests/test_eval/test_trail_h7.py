"""H7: the trail's chain is reachable hop by hop, and the grader wants the cause.

SABOTAGE (each turns the named test red):

* ``write_trail``: drop the note reference from the SPEC-3 maintenance row ->
  ``test_each_hop_is_named_in_the_document_before_it``.
* ``grade_trail``: stop checking distractor blame ->
  ``test_the_grade_needs_the_recalled_lot_and_no_blamed_distractor``.
"""

from __future__ import annotations

import csv
from pathlib import Path

from scripts.eval.tool_calling.fixtures.trail_h7 import write_trail
from scripts.eval.tool_calling.grade_export import grade_trail


def test_each_hop_is_named_in_the_document_before_it(tmp_path: Path) -> None:
    root = tmp_path / "h7-trail"
    key = write_trail(root, 11)
    qc = (root / "qc" / f"batch-{key['batch']}.md").read_text()
    assert "FAIL" in qc
    rows = list(csv.DictReader((root / "samples.csv").open()))
    [row] = [r for r in rows if r["batch"] == key["batch"]]
    assert row["instrument"] == "SPEC-3"
    log = list(csv.DictReader((root / "instruments" / "SPEC-3.csv").open()))
    [service] = [r for r in log if r["date"] == key["service_day"] and r["ref"]]
    note_id = service["ref"].removeprefix("note ")
    [note] = list((root / "notes").rglob(f"{note_id}.md"))
    assert key["lot"] in note.read_text()
    [notice] = [p for p in (root / "vendor" / "notices").iterdir() if key["lot"] in p.read_text()]
    assert key["notice"] in notice.name and "drift" in notice.read_text()


def test_the_grade_needs_the_recalled_lot_and_no_blamed_distractor() -> None:
    key = {
        "batch": "B-16",
        "instrument": "SPEC-3",
        "service_day": "2026-10-10",
        "lot": "L-7672",
        "notice": "VN-640",
        "cause_terms": ["recall", "drift", "wavelength"],
        "distractors": {"freezer": ["FRZ-4", "freezer"], "pipette": ["PIP-7", "pipette"]},
    }
    good = (
        "The SPEC-3 lamp (lot L-7672) is under recall VN-640 for wavelength drift.\n\n"
        "The FRZ-4 alarm was within tolerance and is not the cause."
    )
    assert grade_trail(key, good)["correct"]
    blamed = good + "\n\nThe failure was caused by the freezer temperature alarm on FRZ-4."
    result = grade_trail(key, blamed)
    assert not result["correct"] and result["distractors_blamed"] == ["freezer"]
    assert not grade_trail(key, "The pipette PIP-7 was recalibrated.")["correct"]
    # s2final run 3 (2026-11-04): the distractor named only to rule it out, in the
    # opening sentence. SABOTAGE: drop "not because of" from _CLEARED and this fails.
    cleared = (
        "B-16 failed because of the recalled lamp lot L-7672 (wavelength drift), not "
        "because of freezer storage."
    )
    assert grade_trail(key, cleared)["correct"]
