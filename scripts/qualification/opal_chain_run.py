"""Run one OPAL chain through a prepared CLIO session, then export its attention and score it.

Invoke as a module from the checkout root (it imports its sibling runners)::

    uv run python -m scripts.qualification.opal_chain_run --endpoint URL --session SID \\
        --chains opal_chains.csv --chain-id exp32-chain-0020-hard --condition A --out DIR

Run on the operator's computer against a fresh CLIO session in SPOTTER approval mode with an
explicit model (see ``beta3_opal_run.py``). Steps, each resumable from the output directory:

1. prepare the prompts-only inputs and the operator-only manifest (``beta3_opal_inputs``);
2. send the chosen condition's rounds through the session (``beta3_opal_run``);
3. for each round, read the reply's ``ANSWER:`` line and export its ``;``-separated items as
   ``attn_analytics.py`` CSV rows (the SPOTTER per-item attention view), one CSV per run;
4. compare each parsed answer with the manifest's correct answer, locally. Answers never
   leave this computer; only the prompts are sent.

The answer convention (``ANSWER:`` prefix, ``;`` between items) is the OPAL benchmark's own
output format, read here in the bench driver, never inside CLIO.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from scripts.qualification.beta3_opal_inputs import prepare
from scripts.qualification.beta3_opal_run import run, save

ANSWER_RE = re.compile(r"ANSWER\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
ITEM_DELIMITER = ";"


def answer_span(reply: dict[str, Any]) -> tuple[str, int, int, str] | None:
    """``(part_id, start, end, text)`` of the last ``ANSWER:`` body in the reply's text parts."""
    found = None
    for part in reply.get("parts") or []:
        if part.get("type") != "text" or not part.get("text"):
            continue
        for match in ANSWER_RE.finditer(part["text"]):
            found = (str(part.get("id") or ""), match.start(1), match.end(1), match.group(1))
    return found


def normalize(answer: str) -> str:
    """Compare answers the way ``e2e_example.parse_answer`` reports them."""
    return re.sub(r"\s+", " ", answer.strip().rstrip(".")).casefold()


def export_attention(
    client: httpx.Client,
    *,
    session_id: str,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """One CSV for the run plus a per-round record of what was exported or why not."""
    header: list[str] | None = None
    rows: list[str] = []
    records = []
    for index, turn in enumerate(receipt["turns"]):
        reply = turn.get("reply") or {}
        record: dict[str, Any] = {"round": index + 1, "message_id": reply.get("id")}
        span = answer_span(reply)
        if span is None:
            record["exported"] = False
            record["reason"] = "the reply has no ANSWER: line"
            records.append(record)
            continue
        part_id, start, end, text = span
        round_info = manifest["rounds"][index]
        body = {
            "items": [{"part_id": part_id, "field": "text", "start": start, "end": end}],
            "split": ITEM_DELIMITER,
            "chain_id": manifest["chain_id"],
            "round_no": str(index + 1),
            "task_type": round_info["task"],
        }
        response = client.post(
            f"/v1/sessions/{session_id}/messages/{reply['id']}/attention/items/export", json=body
        )
        response.raise_for_status()
        result = response.json()
        record.update({"answer": text, "exported": bool(result.get("available"))})
        if not result.get("available"):
            record["reason"] = result.get("message")
            record["reason_code"] = result.get("reason")
        else:
            lines = result["csv"].splitlines()
            header = header or lines[:1]
            rows.extend(lines[1:])
            record["items"] = result["rows"]
        records.append(record)
    return "\n".join([*(header or []), *rows]) + ("\n" if header else ""), records


def score(manifest: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Operator-only comparison of each parsed answer with the manifest's correct answer."""
    rounds = []
    for index, turn in enumerate(receipt["turns"]):
        span = answer_span(turn.get("reply") or {})
        expected = manifest["rounds"][index]["correct_answer"]
        got = span[3] if span else None
        rounds.append(
            {
                "round": index + 1,
                "task": manifest["rounds"][index]["task"],
                "got": got,
                "correct_answer": expected,
                "correct": got is not None and normalize(got) == normalize(expected),
            }
        )
    return {
        "chain_id": manifest["chain_id"],
        "correct": sum(r["correct"] for r in rounds),
        "rounds": rounds,
    }


def main() -> None:
    """Prepare, run, export and score one condition of one chain."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--chains", type=Path, required=True, help="opal_chains.csv")
    parser.add_argument("--chain-id", default="exp32-chain-0020-hard")
    parser.add_argument("--condition", choices=["A", "B"], default="A")
    parser.add_argument("--out", type=Path, required=True, help="operator-only output directory")
    parser.add_argument("--timeout", type=float, default=2400, help="seconds per round")
    args = parser.parse_args()

    if not (args.out / "operator-manifest.json").exists():
        prepare(args.chains, args.out, args.chain_id)
    manifest = json.loads((args.out / "operator-manifest.json").read_text(encoding="utf-8"))
    if manifest["chain_id"] != args.chain_id:
        raise ValueError(f"{args.out} holds chain {manifest['chain_id']!r}; use a new directory")
    if args.condition not in manifest["conditions"]:
        raise ValueError(f"Chain {args.chain_id!r} has no condition {args.condition}")

    token = os.environ.get("CLIO_QUALIFICATION_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.endpoint.rstrip("/"), headers=headers, timeout=300) as client:
        receipt = run(
            client,
            endpoint=args.endpoint,
            session_id=args.session,
            input_path=args.out / f"input-{args.condition}.json",
            receipt_path=args.out / f"receipt-{args.condition}.json",
            timeout=args.timeout,
        )
        table, records = export_attention(
            client, session_id=args.session, manifest=manifest, receipt=receipt
        )
    (args.out / f"attention-{args.condition}.csv").write_text(table, encoding="utf-8")
    result = score(manifest, receipt)
    result["attention"] = records
    save(args.out / f"score-{args.condition}.json", result)
    exported = sum(r.get("exported", False) for r in records)
    print(
        json.dumps(
            {
                "chain_id": args.chain_id,
                "condition": args.condition,
                "correct": f"{result['correct']}/{len(result['rounds'])}",
                "attention_rounds_exported": f"{exported}/{len(records)}",
                "out": str(args.out),
            }
        )
    )


if __name__ == "__main__":
    main()
