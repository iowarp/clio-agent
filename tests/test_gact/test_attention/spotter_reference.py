"""The SPOTTER reference implementation, copied for parity tests.

``aggregate`` and ``render``/``line_index`` are verbatim from
spotter-ai-genesis/experiments ``bench/opal/analytics/attn_analytics.py`` and
``render_heatmap.py`` @ ``ebe17be`` (2026-10-23). Only the file reader differs:
those scripts use ``safetensors.safe_open``, which CLIO does not depend on, so
``safe_open`` here is a minimal reader of the same file format.
"""

# ruff: noqa
# fmt: off
from __future__ import annotations

import json
import struct
import sys
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import numpy as np

DECAY = 0.5
RAMP = " .:-=+*#%@"
_DTYPES = {"F32": np.float32, "F64": np.float64, "I32": np.int32, "I64": np.int64}


class _File:
    def __init__(self, path: str) -> None:
        raw = Path(path).read_bytes()
        (size,) = struct.unpack("<Q", raw[:8])
        self._header = json.loads(raw[8 : 8 + size])
        self._data = raw[8 + size :]

    def get_tensor(self, name: str) -> np.ndarray:
        info = self._header[name]
        lo, hi = info["data_offsets"]
        return np.frombuffer(self._data[lo:hi], dtype=_DTYPES[info["dtype"]]).reshape(
            info["shape"]
        )


@contextmanager
def safe_open(path: str, framework: str) -> Iterator[_File]:
    assert framework == "numpy"
    yield _File(path)


# --- verbatim: attn_analytics.py ---------------------------------------------


def aggregate(prov: Path, steps: list[int], n_tokens: int) -> np.ndarray:
    with safe_open(str(prov), "numpy") as g:
        pos, vmax = g.get_tensor("topk_pos"), g.get_tensor("val_all_max")
    score = np.zeros(n_tokens, dtype=np.float64)
    for i, s in enumerate(steps):
        if s >= len(pos):
            continue
        w = DECAY ** i
        p, v = pos[s], vmax[s]
        keep = p >= 0
        np.add.at(score, p[keep], w * v[keep])
    return score


# --- verbatim: render_heatmap.py ---------------------------------------------


def line_index(ids: list[int], tok) -> tuple[list[str], np.ndarray]:
    """Prompt lines, and the line each token belongs to."""
    lines = tok.decode(ids).split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    line_of, ln = np.zeros(len(ids), dtype=int), 0
    for j, t in enumerate(ids):
        line_of[j] = min(ln, len(lines) - 1)
        ln += tok.decode([int(t)]).count("\n")
    return lines, line_of


def render(rows: list[dict], tok, width: int) -> str:
    ids = [int(x) for x in rows[0]["prompt_token_ids"].split()]
    lines, line_of = line_index(ids, tok)

    cols = []
    for r in rows:
        sc = np.fromstring(r["scores"], sep=" ")
        if len(sc) != len(ids):
            sys.exit(f"error: round {r['round_no']} segment {r['segment_index']}: "
                     f"{len(sc)} scores for {len(ids)} tokens")
        per = np.zeros(len(lines))
        np.maximum.at(per, line_of, sc)
        m = per.max() or 1.0
        cols.append([min(9, int(round(9 * x / m))) for x in per])

    head = [f"{rows[0]['chain_id']} round {rows[0]['round_no']} | "
            f"{rows[0]['task_type']} | {rows[0]['model']}"]
    for i, r in enumerate(rows):
        head.append(f"  col{i + 1} = segment {r['segment_index']} "
                    f"{r['segment_text']!r}  (decode steps {r['decode_steps']})")
    head += [f"  ramp {RAMP!r}, level 0..9, each column scaled to its own max",
             f"  {len(ids)} tokens, {len(lines)} lines", "=" * (width + len(cols) + 1)]

    body = ["".join(RAMP[c[i]] for c in cols) + " " + line[:width]
            for i, line in enumerate(lines)]
    return "\n".join(head + body)
