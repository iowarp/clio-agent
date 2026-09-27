"""The Flowcept storage contract the attention query layer reads.

Spec: ``attention-storage-contract.md`` (handed to the vllm-attn-connector
owners). No response lives in one document -- a whole response broke Mongo's
16 MB limit -- so a response is one **summary task** plus one **step task** per
decode step, both ordinary Flowcept tasks in ``tasks`` (the only collections
Flowcept's query API reaches are its own):

summary (``activity_id == "decode_attention"``)::

    used.request_id            "<chatcmpl id>-<hex>"  (":gN" stripped)
    used.num_prompt_tokens     T
    used.num_decode_tokens     G
    generated.attention_summary.segments           [[lo, hi], ...]  partition of [0, T)
    generated.attention_summary.segment_mean_mass  [f, ...]  per segment, mean over steps
    generated.attention_summary.residual_mean      f
    generated.attention_summary.token_index_base   "query" | "produced"
    generated.attention_summary.{top_pct, decode_steps_dropped, restarts, ...}
    generated.attention_summary.attn_sum           BinData f32[T]  (optional)
    generated.attention_summary.prompt_token_ids   BinData i32[T]  (optional)

step (``activity_id == "decode_attention_step"``)::

    used.request_id, used.step
    generated.{token_index, token_id, pos: i32[n], max: f32[n], mean: f32[n],
               head: i16[n], residual}

Arrays are packed little-endian (``numpy.tobytes()``), BSON binary subtype 0.
Parsing is strict: a document off-contract raises
``attention_record_malformed`` naming the field -- never a guessed default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from clio_agent.gact.attention.reasons import AttentionUnavailable

SUMMARY_ACTIVITY = "decode_attention"
STEP_ACTIVITY = "decode_attention_step"
SUMMARY_KEY = "attention_summary"
TOKEN_INDEX_BASES = ("query", "produced")


@dataclass(frozen=True)
class AttentionSummary:
    """One response's summary record."""

    request_id: str
    workflow_id: str
    prompt_tokens: int
    decode_steps: int
    segments: list[tuple[int, int]]
    segment_mean_mass: list[float]
    residual_mean: float
    token_index_base: str
    top_pct: float | None
    health: dict[str, int]
    attn_sum: np.ndarray | None = None
    prompt_token_ids: np.ndarray | None = None
    labels: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttentionStep:
    """One decode step's sparse attention row over the prompt."""

    step: int
    token_index: int
    token_id: int
    pos: np.ndarray
    max: np.ndarray
    mean: np.ndarray
    head: np.ndarray | None
    residual: float


def _malformed(detail: str, **context: Any) -> AttentionUnavailable:
    return AttentionUnavailable("attention_record_malformed", detail, dict(context))


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise _malformed(f"missing {where}.{key}")
    return mapping[key]


def _array(value: Any, dtype: str, where: str) -> np.ndarray:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) % np.dtype(dtype).itemsize:
            raise _malformed(f"{where} byte length {len(raw)} is not a multiple of {dtype}")
        return np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder("<"))
    raise _malformed(f"{where} must be packed binary ({dtype}), got {type(value).__name__}")


def base_request_id(request_id: str) -> str:
    """The provider response id a vLLM request id joins to (``-<hex>`` stripped)."""
    rid = str(request_id).split(":", 1)[0]
    return rid.rsplit("-", 1)[0] if "-" in rid else rid


def parse_summary(doc: dict[str, Any]) -> AttentionSummary:
    """Parse a summary task, or raise a typed reason saying what is missing."""
    used = _require(doc, "used", "task")
    request_id = str(_require(used, "request_id", "used")).split(":", 1)[0]
    generated = doc.get("generated") if isinstance(doc.get("generated"), dict) else {}
    if SUMMARY_KEY not in generated:
        stats = doc.get("attention_stats") or {}
        if isinstance(stats, dict) and stats.get("error"):
            raise AttentionUnavailable(
                "attention_capture_failed", str(stats.get("error")), {"request_id": request_id}
            )
        raise AttentionUnavailable(
            "attention_arrays_not_in_store",
            "the decode_attention record carries no generated.attention_summary"
            + (" (only a file descriptor)" if stats.get("uri") else ""),
            {"request_id": request_id},
        )
    summary = generated[SUMMARY_KEY]
    raw_segments = _require(summary, "segments", SUMMARY_KEY)
    try:
        segments = [(int(seg[0]), int(seg[1])) for seg in raw_segments]
    except (TypeError, ValueError, IndexError) as exc:
        raise _malformed(f"segments unreadable: {exc}") from exc
    mass = [float(v) for v in _require(summary, "segment_mean_mass", SUMMARY_KEY)]
    if len(mass) != len(segments):
        raise _malformed(f"segment_mean_mass has {len(mass)} entries for {len(segments)} segments")
    base = str(_require(summary, "token_index_base", SUMMARY_KEY))
    if base not in TOKEN_INDEX_BASES:
        raise _malformed(f"token_index_base {base!r} not in {TOKEN_INDEX_BASES}")
    prompt_tokens = int(_require(used, "num_prompt_tokens", "used"))
    _check_partition(segments, prompt_tokens)
    labels = summary.get("segment_labels")
    return AttentionSummary(
        request_id=request_id,
        workflow_id=str(doc.get("workflow_id") or ""),
        prompt_tokens=prompt_tokens,
        decode_steps=int(_require(used, "num_decode_tokens", "used")),
        segments=segments,
        segment_mean_mass=mass,
        residual_mean=float(_require(summary, "residual_mean", SUMMARY_KEY)),
        token_index_base=base,
        top_pct=float(summary["top_pct"]) if summary.get("top_pct") is not None else None,
        health={
            key: int(summary.get(key) or 0)
            for key in ("decode_steps_dropped", "decode_steps_nonfinite", "restarts")
        },
        attn_sum=(_array(summary["attn_sum"], "f4", "attn_sum") if "attn_sum" in summary else None),
        prompt_token_ids=(
            _array(summary["prompt_token_ids"], "i4", "prompt_token_ids")
            if "prompt_token_ids" in summary
            else None
        ),
        labels=[str(v) for v in labels] if isinstance(labels, list) else None,
    )


def _check_partition(segments: list[tuple[int, int]], total: int) -> None:
    cursor = 0
    for lo, hi in segments:
        if lo != cursor or hi <= lo:
            raise _malformed(f"segments are not a gap-free partition at [{lo}, {hi})")
        cursor = hi
    if cursor != total:
        raise _malformed(f"segments end at {cursor}, prompt has {total} tokens")


def parse_step(doc: dict[str, Any]) -> AttentionStep:
    """Parse one step task, or raise ``attention_record_malformed``."""
    used = _require(doc, "used", "step task")
    gen = _require(doc, "generated", "step task")
    pos = _array(_require(gen, "pos", "generated"), "i4", "pos")
    vmax = _array(_require(gen, "max", "generated"), "f4", "max")
    mean = _array(_require(gen, "mean", "generated"), "f4", "mean")
    if not len(pos) == len(vmax) == len(mean):
        raise _malformed(
            f"step arrays disagree: pos={len(pos)} max={len(vmax)} mean={len(mean)}",
            step=used.get("step"),
        )
    head = _array(gen["head"], "i2", "head") if "head" in gen else None
    return AttentionStep(
        step=int(_require(used, "step", "used")),
        token_index=int(_require(gen, "token_index", "generated")),
        token_id=int(_require(gen, "token_id", "generated")),
        pos=pos,
        max=vmax,
        mean=mean,
        head=head,
        residual=float(_require(gen, "residual", "generated")),
    )
