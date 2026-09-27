"""Query layer: the descriptor through Flowcept, the arrays from the SafeTensors file.

* the response's descriptor task, joined by response id through Flowcept's own
  ``task_query`` (``used.request_id`` = ``<response id>-<hex>``, an exact
  prefix match);
* the file it names (:mod:`.files` locates and verifies it), read a tensor or a
  row range at a time (:mod:`.safetensors_file`).

Every miss is a typed reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from clio_agent.gact.attention import files
from clio_agent.gact.attention.contract import (
    STEP_TENSORS,
    SUMMARY_ACTIVITY,
    AttentionRecord,
    AttentionStep,
    AttentionSummary,
    check_partition,
    parse_record,
)
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.safetensors_file import SafeTensorsFile


class TaskQuery(Protocol):
    """Anything exposing Flowcept's ``task_query`` shape."""

    def query_tasks(
        self,
        filter: dict[str, Any],
        *,
        projection: list[str] | None = None,
        sort: list[tuple[str, int]] | None = None,
        limit: int = 0,
    ) -> list[dict[str, Any]] | None:
        """Return matching task documents, or ``None`` when the query failed."""
        ...


def _malformed(detail: str, record: AttentionRecord) -> AttentionUnavailable:
    return AttentionUnavailable(
        "attention_record_malformed", detail, {"request_id": record.request_id}
    )


class AttentionStore:
    """Read one response's attention: descriptor, whole-prompt tensors, step rows."""

    def __init__(self, source: TaskQuery, files_dir: str | None = None) -> None:
        """Wrap a Flowcept task-query source; ``files_dir`` overrides the configured mirror."""
        self._source = source
        self._files_dir = files_dir
        self._open: dict[str, SafeTensorsFile] = {}

    def _query(self, filter: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        try:
            rows = self._source.query_tasks(filter, **kwargs)
        except Exception as exc:  # noqa: BLE001 - backend errors become one typed reason
            raise AttentionUnavailable(
                "attention_query_failed", f"{type(exc).__name__}: {exc}", {"filter": str(filter)}
            ) from exc
        if rows is None:
            raise AttentionUnavailable(
                "attention_query_failed", "Flowcept task_query returned no result set"
            )
        return rows

    def record_for(self, response_id: str) -> AttentionRecord:
        """The descriptor for a provider response id (KV-cache group 0)."""
        if not response_id:
            raise AttentionUnavailable("response_id_missing", "the model call has no response id")
        rows = self._query(
            {
                "activity_id": SUMMARY_ACTIVITY,
                "used.request_id": {"$regex": f"^{re.escape(response_id)}-"},
            },
            limit=8,
        )
        if not rows:
            raise AttentionUnavailable(
                "attention_record_not_found",
                f"no {SUMMARY_ACTIVITY} task for response {response_id}",
                {"response_id": response_id},
            )
        rows.sort(key=lambda row: str(row.get("task_id") or ""))
        group0 = [row for row in rows if str(row.get("task_id") or "").endswith(":g0")]
        return parse_record((group0 or rows)[0])

    def _file(self, record: AttentionRecord) -> SafeTensorsFile:
        cached = self._open.get(record.request_id)
        if cached is not None:
            return cached
        path: Path = files.locate(record, self._files_dir)
        files.verify(record, path)
        st = SafeTensorsFile(path)
        header_rid = st.metadata.get("request_id")
        if header_rid and header_rid != record.request_id:
            raise _malformed(f"file header names request {header_rid}", record)
        for name in STEP_TENSORS:
            if st.info(name).shape[0] != record.decode_steps:
                raise _malformed(
                    f"{name} has {st.info(name).shape[0]} rows, record says "
                    f"{record.decode_steps} decode steps",
                    record,
                )
        self._open[record.request_id] = st
        return st

    def summary_for(self, response_id: str) -> AttentionSummary:
        """Descriptor plus the file's whole-prompt tensors, cross-checked."""
        record = self.record_for(response_id)
        st = self._file(record)
        ids = st.read("prompt_token_ids").astype(np.int64)
        if len(ids) != record.prompt_tokens:
            raise _malformed(
                f"prompt_token_ids has {len(ids)} entries, record says {record.prompt_tokens}",
                record,
            )
        raw = st.read("segments")
        if raw.ndim != 2 or raw.shape[1] < 2:
            raise _malformed(f"segments shape {raw.shape} is not [n, 3]", record)
        segments = [(int(lo), int(hi)) for lo, hi in raw[:, :2]]
        check_partition(segments, record.prompt_tokens)
        top_pct = st.metadata.get("top_pct")
        return AttentionSummary(
            record=record,
            prompt_token_ids=ids,
            segments=segments,
            attn_sum=st.read("attn_sum").astype(np.float64),
            attn_peak=st.read("attn_peak").astype(np.float64),
            top_pct=float(top_pct) if top_pct else None,
        )

    def workflow_tokenizer(self, workflow_id: str) -> str:
        """``conf.tokenizer`` of the connector's workflow (the model's HF id), or ``""``."""
        query = getattr(self._source, "query_workflows", None)
        if not workflow_id or not callable(query):
            return ""
        try:
            rows = query({"workflow_id": workflow_id}) or []
        except Exception as exc:  # noqa: BLE001 - backend errors become one typed reason
            raise AttentionUnavailable(
                "attention_query_failed", f"workflow query: {type(exc).__name__}: {exc}"
            ) from exc
        conf = (rows[0].get("conf") or {}) if rows else {}
        return str(conf.get("tokenizer") or conf.get("model") or "")

    def steps(self, summary: AttentionSummary, first: int, last: int) -> list[AttentionStep]:
        """Step rows ``first..last`` inclusive; row ``t`` produced output token ``t``."""
        if last < first:
            return []
        st = self._file(summary.record)
        pos = st.rows("topk_pos", first, last)
        vmax = st.rows("val_all_max", first, last)
        mean = st.rows("val_all_avg", first, last)
        residual = st.rows("topk_residual", first, last).reshape(-1)
        return [
            AttentionStep(
                step=first + i,
                token_index=first + i,
                pos=pos[i],
                max=vmax[i],
                mean=mean[i],
                residual=float(residual[i]),
            )
            for i in range(last - first + 1)
        ]
