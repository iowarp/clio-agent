"""Execution graph shared by flow cards, panels and full-screen views.

The graph separates returning from an agent call from the flow's own acceptance
and delivery. Known research stages are projected from durable action results,
never from an agent's prose. Other flows retain their actual call batches.
"""

from __future__ import annotations

from typing import Any


def execution_view(content: dict[str, Any], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project recorded work, its stopping point and unexecuted delivery stages."""
    if content.get("view"):
        return []  # A published workflow already supplies its dependency graph.
    rows: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, after: list[str], **extra: Any) -> str:
        rows.append({"id": key, "label": label, "status": status, "after": after, **extra})
        return key

    previous = [add("run:start", "Started", "completed", [])]
    research = content.get("flow") == "deep-research"
    actions = list((content.get("steps") or {}).values())
    names = [str(action.get("name") or "") for action in actions]
    directions: list[Any] = []
    if research:
        for action in actions:
            if str(action.get("name") or "").endswith("._plan"):
                plan = action.get("result")
                directions = plan.get("directions") or [] if isinstance(plan, dict) else []
    for event in (content.get("events") or {}).values():
        if event.get("event") == "directions_capped" and isinstance(event.get("kept"), int):
            directions = directions[: max(0, event["kept"])]
    # The research flow assigns d1..dN after its recorded planner action.
    planned = {
        f"key=research:{row.get('id') or f'd{index + 1}'}": (index + 1, row)
        for index, row in enumerate(directions)
        if isinstance(row, dict)
    }
    recorded_directions: set[str] = set()
    stopped = content.get("status") not in {"running", "completed"}
    outcome = content.get("result") if stopped else {}
    outcome = outcome if isinstance(outcome, dict) else {"message": str(outcome)}
    if research and any(name.endswith("._plan") for name in names):
        previous = [add("run:plan", "Plan", "completed", previous)]
    if stopped and not calls:
        previous = [
            add(
                "run:history",
                "Task history incomplete",
                "unknown",
                previous,
                detail="The checkpoint does not record child calls. This does not establish that no children started.",
            )
        ]
    batch: list[str] = []
    group = ""
    rejected_outputs = {
        str(event["critic"]): event.get("problems") or []
        for event in (content.get("events") or {}).values()
        if event.get("event") == "critic_audit_repair" and event.get("critic")
    }
    for index, call in enumerate(call for call in calls if call.get("kind") != "ask"):
        invocation = str(call.get("invocation") or "")
        site = invocation.split(":", 2)[-1].rsplit(":", 2)[0]
        direction = planned.get(site) if call.get("target") == "researcher" else None
        label = (
            f"researcher {direction[0]}"
            if direction
            else "additional researcher"
            if research and planned and call.get("target") == "researcher"
            else str(call.get("target") or "Agent")
        )
        if direction:
            recorded_directions.add(site)
        if call.get("recovered"):
            # Lost journal receipts do not establish a gather or an execution order.
            # A detached branch preserves the known launch without claiming that
            # the parent waited for it before stopping.
            add(
                str(call["invocation"]),
                label,
                "returned_after_stop"
                if call.get("returned_after_stop")
                else "returned"
                if call.get("status") == "completed"
                else str(call.get("status") or "unknown"),
                previous.copy(),
                handle=call.get("handle", ""),
                target=call.get("target", ""),
                kind=call.get("kind", "agent"),
                detail="Recovered from the exact parent assignment's task history. The flow omitted this child from its checkpoint. "
                + (
                    "This child returned after the parent flow had stopped; cleanup did not retire it with the flow."
                    if call.get("returned_after_stop")
                    else "Its execution order was not journaled."
                ),
            )
            continue
        together = str(call.get("together") or "")
        if not together or together != group:
            previous = batch or previous
            batch = []
        group = together
        status = str(call.get("status") or "unknown")
        # Returning means the child executed, not that its claims passed review.
        if status == "completed":
            status = "returned"
        rejection = rejected_outputs.get(str(call.get("handle") or ""))
        if rejection and status == "returned":
            status = "output_rejected"
        key = add(
            str(call.get("invocation") or f"call:{index}"),
            label,
            status,
            previous,
            handle=call.get("handle", ""),
            target=call.get("target", ""),
            invocation=call.get("invocation", ""),
            kind=call.get("kind", "agent"),
            detail=(
                "The child returned, but the flow rejected its audit and requested a repair:\n"
                + "\n".join(str(problem) for problem in rejection)
                if rejection
                else "The child returned. Its output still requires the flow's review."
                if status == "returned"
                else ""
            ),
        )
        batch.append(key)
    previous = batch or previous
    if research and stopped:
        for site, (number, planned_direction) in planned.items():
            if (
                site not in recorded_directions
                and planned_direction.get("closure_kind", "web_research") == "web_research"
            ):
                add(
                    f"planned:{site}",
                    f"Planned research {number}",
                    "unknown",
                    ["run:plan"] if "run:plan" in {row["id"] for row in rows} else ["run:start"],
                    detail=f"Planned question: {planned_direction.get('question', '')}\nNo accepted child for this direction is recorded in this assignment. Whether it started is unconfirmed.",
                )
    if research:
        wrote = any(name.endswith("._write") for name in names)
        reviews = [
            event
            for event in (content.get("events") or {}).values()
            if event.get("event") == "report_review"
        ]
        # Legacy checkpoints can hold the final reviews in the typed outcome.
        observed = outcome.get("observed") or {}
        if not reviews and isinstance(observed, dict):
            reviews = list(observed.get("reviews") or [])
        report_reached = wrote or bool(reviews) or outcome.get("step") == "report"
        if report_reached:
            previous = [
                add("run:draft", "Draft report", "completed" if wrote else "unknown", previous)
            ]
        elif stopped:
            previous = [
                add("run:stop", "Stopped", "failed", previous, detail=outcome.get("message", ""))
            ]
            previous = [add("run:draft", "Draft report", "not_run", previous)]
        review = reviews[-1] if reviews else None
        if review:
            rejected = review.get("status") != "accepted_by_reviewer"
            issues = review.get("issues") or []
            detail = "\n".join(str(issue.get("reason") or "") for issue in issues)
            previous = [
                add(
                    "run:review",
                    "Report review",
                    "rejected" if rejected else "completed",
                    previous,
                    detail=detail,
                )
            ]
        elif stopped:
            previous = [
                add(
                    "run:review",
                    "Report review",
                    "failed" if report_reached else "not_run",
                    previous,
                    detail=outcome.get("message", "") if report_reached else "",
                )
            ]
        persistence_events = [
            event
            for event in (content.get("events") or {}).values()
            if event.get("event") == "report_persistence"
        ]
        if persistence_events:
            persistence = persistence_events[-1]
            status = "completed" if persistence.get("status") == "verified" else "failed"
            add(
                "run:deliver",
                "Save report",
                status,
                previous,
                detail=str(persistence.get("reason") or ""),
            )
        elif stopped:
            add(
                "run:deliver",
                "Save report",
                "not_run",
                previous,
                detail="The flow stopped before report delivery. A returned draft is not a saved, accepted report.",
            )
        elif content.get("status") == "completed":
            result = content.get("result") or {}
            persistence = result.get("persistence") or {} if isinstance(result, dict) else {}
            status = "completed" if persistence.get("status") == "verified" else "unknown"
            add(
                "run:deliver",
                "Save report",
                status,
                previous,
                detail=str(persistence.get("reason") or ""),
            )
        return rows
    if stopped:
        location = str(outcome.get("step") or "").replace("_", " ")
        previous = [
            add(
                "run:stop",
                f"Stopped at {location}" if location else "Stopped",
                "failed",
                previous,
                detail=outcome.get("message", ""),
            )
        ]
        add("run:result", "Result", "not_run", previous)
    elif content.get("status") == "completed":
        add("run:result", "Result", "completed", previous)
    return rows
