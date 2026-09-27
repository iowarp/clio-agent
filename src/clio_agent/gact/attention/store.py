"""Query layer over Flowcept for the connector's attention records.

Thin and typed: two queries, both through Flowcept's own ``task_query`` (no
raw collection access, no file reads) --

* the response's summary task, joined by response id
  (``used.request_id`` = ``<response id>-<hex>``, an exact prefix match);
* the step tasks for a step range, one indexed range query.

Records are parsed by :mod:`.contract`; every miss is a typed reason.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from clio_agent.gact.attention.contract import (
    STEP_ACTIVITY,
    SUMMARY_ACTIVITY,
    AttentionStep,
    AttentionSummary,
    parse_step,
    parse_summary,
)
from clio_agent.gact.attention.reasons import AttentionUnavailable


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


class AttentionStore:
    """Read attention summaries and step rows for one response at a time."""

    def __init__(self, source: TaskQuery) -> None:
        """Wrap a Flowcept task-query source (the configured Flowcept provider)."""
        self._source = source

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

    def summary_for(self, response_id: str) -> AttentionSummary:
        """The summary record for a provider response id (KV-cache group 0)."""
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
        return parse_summary((group0 or rows)[0])

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

    def step_tokens(self, request_id: str) -> list[tuple[int, int, int]]:
        """``(step, token_index, token_id)`` for every step (projected, no arrays)."""
        rows = self._query(
            {"activity_id": STEP_ACTIVITY, "used.request_id": request_id},
            projection=["used.step", "generated.token_index", "generated.token_id"],
            sort=[("used.step", 1)],
        )
        out: list[tuple[int, int, int]] = []
        for row in rows:
            try:
                gen = row["generated"]
                out.append(
                    (int(row["used"]["step"]), int(gen["token_index"]), int(gen["token_id"]))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise AttentionUnavailable(
                    "attention_record_malformed", f"step token projection unreadable: {exc}"
                ) from exc
        if not out:
            raise AttentionUnavailable(
                "attention_steps_missing", f"no {STEP_ACTIVITY} tasks for {request_id}"
            )
        return out

    def steps(self, request_id: str, first: int, last: int) -> list[AttentionStep]:
        """Step rows ``first..last`` inclusive, in order; missing steps are typed."""
        if last < first:
            return []
        rows = self._query(
            {
                "activity_id": STEP_ACTIVITY,
                "used.request_id": request_id,
                "used.step": {"$gte": int(first), "$lte": int(last)},
            },
            sort=[("used.step", 1)],
        )
        steps = [parse_step(row) for row in rows]
        got = {s.step for s in steps}
        missing = [i for i in range(first, last + 1) if i not in got]
        if missing:
            raise AttentionUnavailable(
                "attention_steps_missing",
                f"{len(missing)} of {last - first + 1} steps absent for {request_id}",
                {"request_id": request_id, "first_missing": missing[0]},
            )
        dedup: dict[int, AttentionStep] = {}
        for step in steps:
            dedup.setdefault(step.step, step)
        return [dedup[i] for i in range(first, last + 1)]
