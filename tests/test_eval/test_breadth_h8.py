"""H8: each failed batch's cause is in the records, and the grade is per batch.

SABOTAGE: in ``grade_breadth`` count a batch found when it is merely named (drop the
cause check) -> ``test_the_grade_counts_a_batch_only_with_its_cause``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval.tool_calling.fixtures.breadth_h8 import KINDS, write_breadth
from scripts.eval.tool_calling.grade_export import grade_breadth


def test_every_failed_batch_has_its_evidence(tmp_path: Path) -> None:
    root = tmp_path / "h8-breadth"
    key = write_breadth(root, 3)
    assert {b["kind"] for b in key["batches"]} == set(KINDS)
    files = [p for p in root.rglob("*") if p.is_file()]
    every = "\n".join([p.read_text() for p in files] + [str(p) for p in files])
    for entry in key["batches"]:
        assert "FAIL" in (root / "qc" / f"batch-{entry['batch']}.md").read_text()
        assert all(evidence in every for evidence in entry["ids"])


def test_the_grade_counts_a_batch_only_with_its_cause() -> None:
    key = {
        "batches": [
            {"batch": "B-101", "kind": "standard", "ids": ["STD-5"]},
            {"batch": "B-202", "kind": "lamp", "ids": ["L-9", "VN-1"]},
        ],
        "cause_terms": {"standard": ["expired"], "lamp": ["recall"]},
    }
    text = "- B-101: standard STD-5 had expired.\n- B-202 failed; cause unknown."
    result = grade_breadth(key, text)
    assert result["found"] == {"B-101": True, "B-202": False}
    assert result["batch_recall"] == 0.5 and not result["correct"]
