"""Rebuild the attention fixture from a vllm-attn-connector bundle.

Provenance: attention bundle of job 3237185 (EarthScope offline, 5 turns,
ibm-granite/granite-4.2-30b, vllm-attn-connector ``fix/on-safetensors-e857fd0``
@ 95ab2ac, Flowcept e638b4e2). Response ``chatcmpl-9478ecf2002ea26e`` is a
turn-3 ReAct step (9 messages: three earlier tool calls and results, earlier
turns inline in the question; prompt 15,514 tokens, 329 decode steps). Not run
by the test suite; kept so the fixture is reproducible and its derivation
reviewable.

Outputs:

* ``earthscope_9478ecf2.json.gz``: the real CLIO ``lm.call`` (messages and
  output), the real connector workflow and ``decode_attention`` descriptor, and
  the granite tokenization (ids + offsets) of prompt and output, so tests need
  no 7 MB tokenizer;
* ``<workflow id>/<request id>_g0.safetensors``: the real file with every row kept but each
  ``[G, k]`` row cut to its 64 highest ``val_all_max`` entries (still ascending
  by position). ``prompt_token_ids``, ``attn_sum``, ``attn_peak``, ``segments``,
  ``topk_residual`` and the header metadata are verbatim. The descriptor's
  ``bytes`` / ``sha256`` are rewritten to the cut file's; its ``tensors`` shapes
  say ``k = 64``.

Usage: python build_fixture.py <bundle dir> <tokenizer dir> <out dir>
"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np

from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.safetensors_file import SafeTensorsFile

RESPONSE_ID = "chatcmpl-9478ecf2002ea26e"
KEEP = 64
_NAMES = {np.dtype("<f4"): "F32", np.dtype("<i4"): "I32"}


def _write_safetensors(path: Path, tensors: dict[str, np.ndarray], meta: dict[str, str]) -> None:
    header: dict[str, object] = {"__metadata__": meta}
    offset = 0
    blobs = []
    for name, arr in tensors.items():
        raw = np.ascontiguousarray(arr).tobytes()
        header[name] = {
            "dtype": _NAMES[arr.dtype],
            "shape": list(arr.shape),
            "data_offsets": [offset, offset + len(raw)],
        }
        blobs.append(raw)
        offset += len(raw)
    head = json.dumps(header, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 8)
    path.write_bytes(struct.pack("<Q", len(head)) + head + b"".join(blobs))


def main(bundle: Path, tok_dir: Path, out: Path) -> None:
    """Write the fixture JSON (gzip) and the cut SafeTensors file."""
    tasks = [
        json.loads(line)
        for line in (bundle / "full" / "tasks_3237185.json").open(encoding="utf-8")
        if line.strip()
    ]
    workflows = [
        json.loads(line)
        for line in (bundle / "full" / "workflows_3237185.json").open(encoding="utf-8")
        if line.strip()
    ]
    call = next(
        t
        for t in tasks
        if t.get("subtype") == "ai_model_invocation"
        and t["custom_metadata"]["clio"]["response_id"] == RESPONSE_ID
    )
    record = next(
        t
        for t in tasks
        if t.get("subtype") == "decode_attention"
        and t["used"]["request_id"].startswith(RESPONSE_ID + "-")
    )
    workflow = next(w for w in workflows if w["workflow_id"] == record["workflow_id"])
    clio = call["custom_metadata"]["clio"]
    payload = clio["payload"]
    request_id = record["used"]["request_id"]
    source = SafeTensorsFile(
        bundle / "full" / "files" / record["workflow_id"] / f"{request_id}_g0.safetensors"
    )
    vmax = source.read("val_all_max")
    order = np.sort(np.argsort(-vmax, axis=1)[:, :KEEP], axis=1)
    rows = np.arange(vmax.shape[0])[:, None]
    cut = {
        "prompt_token_ids": source.read("prompt_token_ids"),
        "attn_sum": source.read("attn_sum"),
        "attn_peak": source.read("attn_peak"),
        "segments": source.read("segments"),
        "topk_pos": source.read("topk_pos")[rows, order],
        "topk_head": source.read("topk_head")[rows, order],
        "val_all_max": vmax[rows, order],
        "val_all_avg": source.read("val_all_avg")[rows, order],
        "topk_residual": source.read("topk_residual"),
    }
    file_path = out / record["workflow_id"] / f"{request_id}_g0.safetensors"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    _write_safetensors(file_path, cut, source.metadata)
    raw = file_path.read_bytes()
    stats = dict(record["attention_stats"])
    stats["bytes"] = len(raw)
    stats["sha256"] = hashlib.sha256(raw).hexdigest()
    stats["tensors"] = {
        name: {"shape": list(arr.shape), "dtype": "int32" if arr.dtype.kind == "i" else "float32"}
        for name, arr in cut.items()
    }
    descriptor = {
        key: record[key] for key in ("task_id", "activity_id", "subtype", "workflow_id", "used")
    }
    descriptor["type"] = "task"
    descriptor["attention_stats"] = stats

    renderer = ChatRenderer.from_dir(tok_dir, identity="ibm-granite/granite-4.2-30b")
    prompt = renderer.render_encoded(payload["messages"])
    output = renderer.encode(payload["content"])
    assert prompt.ids == cut["prompt_token_ids"].tolist(), "render must match the capture"
    assert len(output.ids) + 1 == record["used"]["num_decode_tokens"], "G = output + stop"
    fixture = {
        "provenance": "attention bundle job 3237185, connector 95ab2ac; see build_fixture.py",
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
            "workflow_id": workflow["workflow_id"],
            "conf": {"tokenizer": workflow["conf"]["tokenizer"]},
            "attention_config": workflow["attention_config"],
        },
        "descriptor": descriptor,
        "prompt": {"ids": prompt.ids, "offsets": [list(o) for o in prompt.offsets]},
        "output": {"ids": output.ids, "offsets": [list(o) for o in output.offsets]},
    }
    with gzip.open(out / "earthscope_9478ecf2.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(fixture, fh, separators=(",", ":"))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
