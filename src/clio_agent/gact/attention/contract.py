"""The connector's attention record: a Flowcept descriptor task plus a SafeTensors file.

vllm-attn-connector writes one SafeTensors file per (vLLM request, KV-cache
group) and one Flowcept task that points at it (``SCHEMA.md`` of the attention
bundles; verified on job 3237185, 29/29 records)::

    task_id                      "<vllm request id>:g<group>"
    activity_id                  "decode_attention"
    workflow_id                  the connector's workflow (conf.tokenizer, attention_config)
    used.request_id              "<chatcmpl id>-<hex>"   (joins CLIO's response id by prefix)
    used.num_prompt_tokens       T
    used.num_decode_tokens       G
    attention_stats.uri          "file://<out_dir>/<workflow_id>/<request id>_g<group>.safetensors"
    attention_stats.{bytes, sha256, segment_mode, kv_cache_group_id,
                     decode_steps_dropped, decode_steps_unscored,
                     decode_steps_nonfinite, restarts}
    attention_stats.error        set (with uri null) when the write failed

File tensors: ``prompt_token_ids [T]``, ``attn_sum [T]``, ``attn_peak [T]``,
``segments [n, 3]`` as ``(lo, hi, keep)``, and per decode step ``topk_pos``,
``val_all_max``, ``val_all_avg``, ``topk_head`` ``[G, k]`` plus
``topk_residual [G, 1]``. Row ``t`` is the step that produced output token
``t``; the last row of a stopped request is the stop token.

Parsing is strict: a document off-contract raises
``attention_record_malformed`` naming the field -- never a guessed default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from clio_agent.gact.attention.reasons import AttentionUnavailable

SUMMARY_ACTIVITY = "decode_attention"
HEALTH_KEYS = (
    "decode_steps_dropped",
    "decode_steps_unscored",
    "decode_steps_nonfinite",
    "restarts",
)
STEP_TENSORS = ("topk_pos", "val_all_max", "val_all_avg", "topk_residual")


@dataclass(frozen=True)
class AttentionRecord:
    """One response's descriptor task (no arrays; those live in the file)."""

    request_id: str
    workflow_id: str
    prompt_tokens: int
    decode_steps: int
    uri: str
    sha256: str
    size: int | None
    segment_mode: str
    health: dict[str, int]

    @property
    def clean(self) -> bool:
        """Whether every decode step has exactly one row (no health counter set)."""
        return not any(self.health.values())


@dataclass(frozen=True)
class AttentionSummary:
    """A record plus the file's whole-prompt tensors."""

    record: AttentionRecord
    prompt_token_ids: np.ndarray
    segments: list[tuple[int, int]]
    attn_sum: np.ndarray
    attn_peak: np.ndarray
    top_pct: float | None

    @property
    def request_id(self) -> str:
        """The vLLM request id."""
        return self.record.request_id

    @property
    def prompt_tokens(self) -> int:
        """``T``, the scored prompt length."""
        return self.record.prompt_tokens

    @property
    def decode_steps(self) -> int:
        """``G``, rows per step tensor."""
        return self.record.decode_steps

    @property
    def health(self) -> dict[str, int]:
        """The connector's health counters (all 0 on a clean capture)."""
        return self.record.health


@dataclass(frozen=True)
class AttentionStep:
    """One decode step's sparse attention row over the prompt."""

    step: int
    token_index: int
    pos: np.ndarray
    max: np.ndarray
    mean: np.ndarray
    residual: float


def _malformed(detail: str, **context: Any) -> AttentionUnavailable:
    return AttentionUnavailable("attention_record_malformed", detail, dict(context))


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise _malformed(f"missing {where}.{key}")
    return mapping[key]


def base_request_id(request_id: str) -> str:
    """The provider response id a vLLM request id joins to (``-<hex>`` stripped)."""
    rid = str(request_id).split(":", 1)[0]
    return rid.rsplit("-", 1)[0] if "-" in rid else rid


def parse_record(doc: dict[str, Any]) -> AttentionRecord:
    """Parse a descriptor task, or raise a typed reason saying what is wrong."""
    used = _require(doc, "used", "task")
    request_id = str(_require(used, "request_id", "used")).split(":", 1)[0]
    stats = _require(doc, "attention_stats", "task")
    if not isinstance(stats, dict):
        raise _malformed("attention_stats is not an object", request_id=request_id)
    if stats.get("error") or not stats.get("uri"):
        raise AttentionUnavailable(
            "attention_capture_failed",
            str(stats.get("error") or "the record has no file uri"),
            {"request_id": request_id},
        )
    fmt = str(stats.get("format") or "")
    if fmt != "safetensors":
        raise _malformed(f"attention_stats.format {fmt!r} is not 'safetensors'")
    try:
        health = {key: int(stats.get(key) or 0) for key in HEALTH_KEYS}
        size = int(stats["bytes"]) if stats.get("bytes") is not None else None
        prompt_tokens = int(_require(used, "num_prompt_tokens", "used"))
        decode_steps = int(_require(used, "num_decode_tokens", "used"))
    except (TypeError, ValueError) as exc:
        raise _malformed(f"descriptor counts unreadable: {exc}", request_id=request_id) from exc
    return AttentionRecord(
        request_id=request_id,
        workflow_id=str(doc.get("workflow_id") or ""),
        prompt_tokens=prompt_tokens,
        decode_steps=decode_steps,
        uri=str(stats["uri"]),
        sha256=str(stats.get("sha256") or ""),
        size=size,
        segment_mode=str(stats.get("segment_mode") or ""),
        health=health,
    )


def check_partition(segments: list[tuple[int, int]], total: int) -> None:
    """Segments must tile ``[0, total)`` with no gap or overlap."""
    cursor = 0
    for lo, hi in segments:
        if lo != cursor or hi <= lo:
            raise _malformed(f"segments are not a gap-free partition at [{lo}, {hi})")
        cursor = hi
    if cursor != total:
        raise _malformed(f"segments end at {cursor}, prompt has {total} tokens")
