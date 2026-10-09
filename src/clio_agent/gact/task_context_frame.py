"""Keep the existing context truth frame aligned with committed task delivery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.runtime.retention import ledger_guard


def commit_task_context_frame(state: Any) -> None:
    """Replace staged task text with the exact injections surviving collection/veto."""
    from clio_agent.gact.enrichment import _estimate_context_tokens

    frame = getattr(state, "context_frame", None)
    if frame is None:
        return
    with ledger_guard(state.app):
        previous = frame["items"]
        removed_tokens = sum(
            item.get("tokens_estimated", 0)
            for item in previous
            if item.get("kind") == "injection" and item.get("source_id") == "task_results"
        )
        retained = [
            item
            for item in previous
            if not (item.get("kind") == "injection" and item.get("source_id") == "task_results")
        ]
        committed = [
            {
                "kind": "injection",
                "source_id": source,
                "included": True,
                "reason": "clio_addition",
                "tokens_estimated": _estimate_context_tokens(text),
                "metadata": {"actor": "algorithm", "text": text},
            }
            for source, text in state.injections
            if source == "task_results"
        ]
        frame["items"] = [*retained, *committed]
        frame["tokens_estimated"] = max(
            0,
            frame["tokens_estimated"]
            - removed_tokens
            + sum(item["tokens_estimated"] for item in committed),
        )
        frame["metadata"]["task_delivery_phase"] = "committed"
        frame["updated_at"] = datetime.now(timezone.utc).isoformat()
