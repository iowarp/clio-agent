"""``optimize-scan``: one child per session, in parallel, each returning its findings."""

from __future__ import annotations

import json
from typing import Any

from clio_agent.sdk import Flow


class OptimizeScan(Flow):
    """Scan sessions for /optimize: the optimize-scanner reads each in its own child.

    Input: ``{"sessions": [{"session_id", "title"}...] | [ids], "declined": [...]}``,
    the scope ``optimize_scope`` showed. Every session's outcome comes back: its
    findings, or why its scan did not finish; none is dropped.
    """

    id = "optimize-scan"
    description = (
        "Reads sessions for /optimize in parallel, one optimize-scanner child per session "
        "(on the optimize fleet role), and returns each session's findings with evidence. "
        "Input: {sessions: the scope optimize_scope showed, declined: findings declined "
        "before}. Options: concurrency (how many sessions are read at once, default 4)."
    )
    shape = """railroad-beta
optimize_scan = sequence(choice(terminal("session 1"), terminal("session 2"), terminal("session n")),
                         terminal("findings"));"""
    concurrency = 4

    async def forward(self, task: Any, ctx: Any) -> Any:
        spec = task if isinstance(task, dict) else json.loads(str(task or "{}"))
        sessions = [
            row if isinstance(row, dict) else {"session_id": str(row), "title": ""}
            for row in spec.get("sessions") or []
        ]
        declined = spec.get("declined") or []
        known = (
            "\n\nDeclined before (report again only if this session shows it again):\n"
            + json.dumps(declined)
            if declined
            else ""
        )
        calls = []
        for row in sessions:
            call = ctx.call(
                "optimize-scanner",
                f"Scan session {row['session_id']} ({row.get('title') or 'untitled'}).{known}",
            )
            call.label = f"Scan: {row.get('title') or row['session_id']}"
            calls.append(call)
        results = await ctx.gather(calls, limit=self.concurrency, return_exceptions=True)
        scanned, unfinished = [], []
        for row, result in zip(sessions, results, strict=True):
            answer = getattr(result, "answer", None)
            if answer is None:
                unfinished.append({"session_id": row["session_id"], "reason": str(result)})
            else:
                scanned.append({"session_id": row["session_id"], "findings": answer})
        report = {"scanned": scanned, "unfinished": unfinished}
        return {"answer": json.dumps(report, ensure_ascii=False)}
