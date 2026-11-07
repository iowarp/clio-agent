#!/usr/bin/env python3
"""Measure SPOTTER's reviews of the OPAL sessions: tools used, errors, alerts and lag.

Usage: spotter_eval.py <setup-name> [endpoint]
Reads $ROOT/evidence/opal/opal-sessions.json and writes $ROOT/evidence/spotter-eval/<setup>.json.
Stdlib only, read-only against CLIO's REST API (no answers or operator files are read).
"""

import datetime
import json
import os
import sys
import urllib.request

PURPOSE_TOOLS = ("review_activity", "attention_sources")


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.load(response)


def when(stamp):
    if not stamp:
        return None
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def review_rows(base, kind, watched):
    messages = get(base, "/v1/sessions/%s/messages" % watched).get("messages", [])
    turn_ends = sorted(
        when(m.get("updated_at") or m.get("created_at"))
        for m in messages
        if m.get("role") == "assistant" and m.get("stop_reason")
    )
    rows = []
    for task in get(base, "/v1/sessions/%s/agent-tasks" % watched).get("tasks", []):
        child = task.get("child_session_id")
        choice = task.get("model_choice") or {}
        reviews = get(base, "/v1/sessions/%s/messages" % child).get("messages", [])
        for review in reviews:
            if review.get("role") != "assistant":
                continue
            parts = review.get("parts", [])
            calls = [p.get("tool_name") or "" for p in parts if p.get("type") == "tool_call"]
            errors = [
                p.get("tool_name") or ""
                for p in parts
                if p.get("type") == "tool_result" and p.get("is_error")
            ]
            started = when(review.get("created_at"))
            ended = when(review.get("updated_at")) if review.get("stop_reason") else None
            woke_for = max((t for t in turn_ends if started and t <= started), default=None)
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            rows.append(
                {
                    "watched": kind,
                    "watched_session": watched,
                    "spotter_session": child,
                    "review_message": review.get("id"),
                    "model": choice.get("ran"),
                    "model_source": choice.get("source"),
                    "finished": bool(review.get("stop_reason")),
                    "started_at": review.get("created_at"),
                    "duration_s": (ended - started).total_seconds() if ended and started else None,
                    "lag_s": (ended - woke_for).total_seconds() if ended and woke_for else None,
                    "tool_calls": len(calls),
                    "tools": calls,
                    "purpose_tools": [c for c in calls if c in PURPOSE_TOOLS],
                    "attention_calls": [c for c in calls if "attention" in c],
                    "alerts": [c for c in calls if "alert" in c],
                    "tool_errors": errors,
                    "verdict_excerpt": " ".join(text.split())[:400],
                }
            )
    return rows


def summary(rows):
    done = [r for r in rows if r["finished"]]

    def mean(key):
        values = [r[key] for r in done if r[key] is not None]
        return round(sum(values) / len(values), 1) if values else None

    return {
        "reviews": len(rows),
        "finished": len(done),
        "mean_tool_calls": mean("tool_calls"),
        "mean_duration_s": mean("duration_s"),
        "mean_lag_s": mean("lag_s"),
        "used_review_activity": sum("review_activity" in r["purpose_tools"] for r in done),
        "used_attention_sources": sum("attention_sources" in r["purpose_tools"] for r in done),
        "tool_errors": sum(len(r["tool_errors"]) for r in done),
        "alerts": sum(len(r["alerts"]) for r in done),
        "models": sorted({json.dumps(r["model"], sort_keys=True) for r in rows if r["model"]}),
    }


def main():
    setup = sys.argv[1]
    base = (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8100").rstrip("/")
    root = os.environ["ROOT"]
    sessions = json.load(open(os.path.join(root, "evidence", "opal", "opal-sessions.json")))
    rows = []
    for kind, sid in sorted(sessions.items()):
        if (kind.startswith("chain_") or kind == "broad") and isinstance(sid, str):
            rows.extend(review_rows(base, kind, sid))
    out = {
        "setup": setup,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": summary(rows),
        "by_session": {
            kind: summary([r for r in rows if r["watched"] == kind])
            for kind in sorted({r["watched"] for r in rows})
        },
        "reviews": rows,
    }
    os.makedirs(os.path.join(root, "evidence", "spotter-eval"), exist_ok=True)
    path = os.path.join(root, "evidence", "spotter-eval", setup + ".json")
    with open(path, "w") as handle:
        json.dump(out, handle, indent=2)
    print(json.dumps({"path": path, **out["summary"]}, indent=2))


if __name__ == "__main__":
    main()
