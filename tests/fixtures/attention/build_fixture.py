"""Rebuild ``earthscope_a00d067a.json.gz`` from the recorded SPOTTER-AI run.

Provenance: Flowcept dump of job 3188205 (earthscope offline, granite-4.2-30b,
vllm-attn-connector cb39257, clio-agent 0d69f02a). Response
``chatcmpl-a00d067a9ecb0bf5`` is a turn-1 ReAct step (prompt 13,296 tokens,
435 decode steps, all 4 connector parts present). Not run by the test suite;
kept so the fixture is reproducible and its derivation reviewable.

Inputs (produced by streaming the dump, see the attention PR):
  ``lm_calls.ndjson``     the run's CLIO lm.call records
  ``parts_*.bson``        the response's decode_attention part documents
  ``granite/``            ibm-granite/granite-4.2-30b tokenizer files (no weights)

Conversion to the proposed storage contract (``gact/attention/contract.py``):

* summary: ``segments`` and ``prompt_token_ids`` verbatim; ``attn_sum`` verbatim;
  ``segment_mean_mass`` = per-segment sum of ``attn_sum`` / G (``attn_sum`` is the
  full mean row summed over steps, so this is exact); ``residual_mean`` = mean of
  the recorded ``topk_residual``;
* steps: ``pos`` / ``max`` / ``mean`` / ``head`` / ``residual`` verbatim from the
  recorded ``[G, k]`` rows (``-1`` padding dropped). cb39257 does not record the
  generated token ids, so ``token_index = step`` and ``token_id`` comes from the
  model tokenizer (the trailing extra step is the EOS query). The contract asks
  the connector to record both; the run's induction pattern (top prompt tokens at
  step t match output token t 18% and t+1 15%, but t-1 only 6%) supports this
  "query" reading.

Usage: python build_fixture.py <scratch dir with ds/ and tok/granite> <out.json.gz>
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import bson
from bson import json_util

from clio_agent.gact.attention.chat_render import ChatRenderer

RESPONSE_ID = "chatcmpl-a00d067a9ecb0bf5"
STEP_WINDOW = (262, 272)  # the thought's first sentence, tokens 262..271
EOS_ID = 100257


def main(scratch: Path, out: Path) -> None:
    """Write the fixture JSON (gzip)."""
    renderer = ChatRenderer.from_dir(
        scratch / "tok" / "granite", identity="ibm-granite/granite-4.2-30b"
    )
    docs = []
    for path in sorted((scratch / "ds").glob("parts_*.bson")):
        docs.extend(bson.decode_all(path.read_bytes()))
    parts = {
        d["custom_metadata"]["part_index"]: d
        for d in docs
        if d["custom_metadata"]["parent_request_id"].startswith(RESPONSE_ID + "-")
    }
    calls = [
        json_util.loads(line)
        for line in (scratch / "ds" / "lm_calls.ndjson").open(encoding="utf-8")
    ]
    call = next(c for c in calls if c["custom_metadata"]["clio"]["response_id"] == RESPONSE_ID)
    clio = call["custom_metadata"]["clio"]
    payload = clio["payload"]
    p0 = parts[0]
    cm = p0["custom_metadata"]
    total_steps = int(cm["decode_steps_total"])
    rows: dict[int, dict] = {}
    residuals: list[float] = []
    for index in sorted(parts):
        part = parts[index]
        offset = int(part["custom_metadata"]["step_offset"])
        gen = part["generated"]
        for i, pos in enumerate(gen["topk_pos"]):
            residuals.append(float(gen["topk_residual"][i][0]))
            step = offset + i
            if STEP_WINDOW[0] <= step < STEP_WINDOW[1]:
                keep = [j for j, p in enumerate(pos) if p >= 0]
                rows[step] = {
                    "pos": [int(pos[j]) for j in keep],
                    "max": [gen["val_all_max"][i][j] for j in keep],
                    "mean": [gen["val_all_avg"][i][j] for j in keep],
                    "head": [int(gen["topk_head"][i][j]) for j in keep],
                    "residual": float(gen["topk_residual"][i][0]),
                }
    assert len(residuals) == total_steps, (len(residuals), total_steps)
    attn_sum = p0["generated"]["attn_sum"]
    segments = [[int(lo), int(hi)] for lo, hi, _k in p0["generated"]["segments"]]
    prompt = renderer.render_encoded(payload["messages"])
    output = renderer.encode(payload["content"])
    out_ids = output.ids + [EOS_ID]
    assert prompt.ids == list(p0["used"]["prompt_token_ids"]), "render must match the capture"
    fixture = {
        "provenance": "flowcept job 3188205, vllm-attn-connector cb39257, see build_fixture.py",
        "lm_call": {
            "event_id": clio["event_id"],
            "session_id": clio["session_id"],
            "turn_id": clio["turn_id"],
            "model": payload["model"],
            "response_id": RESPONSE_ID,
            "messages": payload["messages"],
            "content": payload["content"],
        },
        "workflow": {
            "workflow_id": p0["workflow_id"],
            "conf": {"tokenizer": "ibm-granite/granite-4.2-30b"},
        },
        "request_id": cm["parent_request_id"].split(":")[0],
        "prompt": {"ids": prompt.ids, "offsets": [list(o) for o in prompt.offsets]},
        "output": {"ids": output.ids, "offsets": [list(o) for o in output.offsets]},
        "summary": {
            "num_prompt_tokens": len(prompt.ids),
            "num_decode_tokens": total_steps,
            "segments": segments,
            "segment_mean_mass": [sum(attn_sum[lo:hi]) / total_steps for lo, hi in segments],
            "residual_mean": sum(residuals) / len(residuals),
            "top_pct": float(cm["top_pct"]),
            "attn_sum": attn_sum,
            "health": {
                k: int(cm.get(k) or 0)
                for k in ("decode_steps_dropped", "decode_steps_nonfinite", "restarts")
            },
        },
        "step_tokens": [
            [s, s, out_ids[s] if s < len(out_ids) else EOS_ID] for s in range(total_steps)
        ],
        "steps": {str(k): v for k, v in sorted(rows.items())},
    }
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(fixture, fh, separators=(",", ":"))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
