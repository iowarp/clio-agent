"""Controlled replay inputs preserve the chain without leaking evaluator answers."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.qualification.beta3_opal_inputs import ANCHOR, CHAIN_ID, INJECTION, prepare


def reference(path: Path, rounds: int = 6) -> None:
    """Write a synthetic reference only for testing the preparation boundary."""
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "chain_id",
                "round_no",
                "task_type",
                "prompt",
                "correct_answer",
                "explanation",
                "tool_ranges",
            ],
        )
        writer.writeheader()
        for number in range(1, rounds + 1):
            writer.writerow(
                {
                    "chain_id": CHAIN_ID,
                    "round_no": number,
                    "task_type": "analogue_search" if number == 5 else "measurement",
                    "prompt": f"Round {number}: carry your previous answer.\n>>> embedding_lookup()\n table\n"
                    + ANCHOR,
                    "correct_answer": "EVALUATOR_SECRET",
                    "explanation": "BLIND_REVIEW_MUST_NOT_SEE",
                    "tool_ranges": "unverified token coordinates",
                }
            )


def test_prompts_keep_actual_conversation_dependencies_and_one_localized_change(
    tmp_path: Path,
) -> None:
    source, output = tmp_path / "source.csv", tmp_path / "operator"
    reference(source)
    manifest = prepare(source, output)
    clean = json.loads((output / "input-A.json").read_text())
    changed = json.loads((output / "input-B.json").read_text())
    assert len(clean["prompts"]) == len(changed["prompts"]) == 6
    assert [
        index
        for index, (a, b) in enumerate(zip(clean["prompts"], changed["prompts"], strict=True))
        if a != b
    ] == [4]
    assert INJECTION in changed["prompts"][4]
    assert all("carry your previous answer" in text for text in clean["prompts"])
    serialized = json.dumps([clean, changed])
    assert not any(
        secret in serialized
        for secret in ("EVALUATOR_SECRET", "BLIND_REVIEW_MUST_NOT_SEE", "tool_ranges", "conditions")
    )
    assert manifest["rounds"][0]["correct_answer"] == "EVALUATOR_SECRET"
    assert not manifest["inference_tested"]


def test_incomplete_chain_and_existing_output_are_refused(tmp_path: Path) -> None:
    source, output = tmp_path / "source.csv", tmp_path / "operator"
    reference(source, rounds=5)
    with pytest.raises(ValueError, match="exactly rounds"):
        prepare(source, output)
    assert not output.exists()
    reference(source)
    output.mkdir()
    with pytest.raises(FileExistsError):
        prepare(source, output)
