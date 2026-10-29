"""The OPAL chain driver exports each round's answer items and scores answers locally."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest

from scripts.qualification.beta3_opal_inputs import prepare
from scripts.qualification.opal_chain_run import answer_span, export_attention, normalize, score

HEADER = "chain_id,round_no,task_type,model,segment_index,segment_text,decode_steps,n_prompt_tokens,prompt_token_ids,scores"


def _reply(mid: str, text: str) -> dict:
    return {
        "id": mid,
        "role": "assistant",
        "parts": [{"id": "p-" + mid, "type": "text", "text": text}],
    }


def test_answer_span_takes_the_last_answer_line_of_the_text_parts() -> None:
    reply = {
        "id": "m",
        "parts": [
            {"id": "t", "type": "thinking", "text": "ANSWER: draft"},
            {
                "id": "a",
                "type": "text",
                "text": "Checked both tables.\nANSWER: NIL 373; DIVERGES\n",
            },
        ],
    }
    part_id, start, end, text = answer_span(reply)
    assert (part_id, text) == ("a", "NIL 373; DIVERGES")
    assert reply["parts"][1]["text"][start:end] == text
    assert answer_span(_reply("x", "no answer here")) is None


def test_export_posts_items_split_at_semicolon_and_joins_one_csv() -> None:
    posted: list[dict] = []

    def server(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posted.append({"path": request.url.path, **body})
        if "m2" in request.url.path:
            return httpx.Response(
                200,
                json={"available": False, "reason": "output_alignment_mismatch", "message": "no"},
            )
        row = f"exp,{body['round_no']},{body['task_type']},model,0,x,1,2,3 4,0.5 1"
        return httpx.Response(200, json={"available": True, "rows": 1, "csv": f"{HEADER}\n{row}\n"})

    manifest = {
        "chain_id": "exp",
        "rounds": [{"task": "dose"}, {"task": "peak"}, {"task": "search"}],
    }
    receipt = {
        "turns": [
            {"reply": _reply("m1", "ANSWER: 100")},
            {"reply": _reply("m2", "ANSWER: 100; AGREES")},
            {"reply": _reply("m3", "I could not decide.")},
        ]
    }
    with httpx.Client(transport=httpx.MockTransport(server), base_url="http://clio") as client:
        table, records = export_attention(
            client, session_id="s", manifest=manifest, receipt=receipt
        )
    assert table.splitlines() == [HEADER, "exp,1,dose,model,0,x,1,2,3 4,0.5 1"]
    assert posted[0]["path"] == "/v1/sessions/s/messages/m1/attention/items/export"
    assert posted[0]["split"] == ";" and posted[0]["items"] == [
        {"part_id": "p-m1", "field": "text", "start": 8, "end": 11}
    ]
    assert posted[1]["round_no"] == "2" and posted[1]["task_type"] == "peak"
    assert [r["exported"] for r in records] == [True, False, False]
    assert records[1]["reason_code"] == "output_alignment_mismatch"
    assert records[2]["reason"] == "the reply has no ANSWER: line"
    assert len(posted) == 2


def test_score_compares_normalized_answers() -> None:
    manifest = {
        "chain_id": "exp",
        "rounds": [
            {"task": "a", "correct_answer": "NIL 373; DIVERGES"},
            {"task": "b", "correct_answer": "100"},
        ],
    }
    receipt = {
        "turns": [
            {"reply": _reply("m1", "ANSWER: nil 373;  diverges.")},
            {"reply": _reply("m2", "ANSWER: 50")},
        ]
    }
    result = score(manifest, receipt)
    assert result["correct"] == 1
    assert [r["correct"] for r in result["rounds"]] == [True, False]
    assert normalize(" MAGIC 75@500. ") == "magic 75@500"


def test_any_chain_prepares_a_clean_replay_only(tmp_path: Path) -> None:
    source = tmp_path / "chains.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
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
        for number in (1, 2):
            writer.writerow(
                {
                    "chain_id": "exp114-chain-0001-low",
                    "round_no": number,
                    "task_type": "t",
                    "prompt": f"Round {number}",
                    "correct_answer": "SECRET",
                    "explanation": "e",
                    "tool_ranges": "",
                }
            )
    manifest = prepare(source, tmp_path / "out", "exp114-chain-0001-low")
    assert manifest["conditions"] == {"A": "clean"} and "injection" not in manifest
    assert len(json.loads((tmp_path / "out" / "input-A.json").read_text())["prompts"]) == 2
    assert not (tmp_path / "out" / "input-B.json").exists()
    assert "SECRET" not in (tmp_path / "out" / "input-A.json").read_text()
    with pytest.raises(ValueError, match="no gap"):
        prepare(source, tmp_path / "other", "missing-chain")
