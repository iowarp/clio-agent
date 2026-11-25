"""Summarize a CLIO session for the NDP ledger: skills, calls, failures and the reply.

    uv run python scripts/cloud/ndp_run_summary.py sess_... [--agent http://127.0.0.1:8787]

Prints Markdown: the skills the agent loaded (its own choices on a plain request),
tool-call counts, each distinct failure with its text, and the final reply. Reads the
session through the GACT API only.
"""

from __future__ import annotations

import argparse
import collections
import json
import urllib.request


def _get(base: str, route: str) -> dict:
    with urllib.request.urlopen(base + route, timeout=60) as response:
        return json.load(response)


def _text(part: dict) -> str:
    content = part.get("content") or []
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("--agent", default="http://127.0.0.1:8787")
    parser.add_argument("--reply-chars", type=int, default=6000)
    args = parser.parse_args()
    session = _get(args.agent, f"/v1/sessions/{args.session}")
    listed = _get(args.agent, f"/v1/sessions/{args.session}/messages?limit=1000")
    messages = listed.get("messages", listed)
    calls: collections.Counter[str] = collections.Counter()
    skills: list[str] = []
    failures: dict[str, tuple[str, int]] = {}
    replies: list[str] = []
    for message in messages:
        for part in message.get("parts", []):
            kind = part.get("type")
            if kind == "tool_call":
                calls[part.get("tool_name", "")] += 1
                if part.get("tool_name") == "load_skill":
                    skills.append((part.get("input") or {}).get("skill_id", ""))
            elif kind == "tool_result" and part.get("is_error"):
                text = _text(part)[:240].replace("\n", " ")
                key = f"{part.get('tool_name')}: {text[:90]}"
                name, count = failures.get(key, (text, 0))
                failures[key] = (name, count + 1)
            elif kind == "text" and message.get("role") == "assistant" and part.get("text"):
                replies.append(part["text"])
    print(
        f"Session `{args.session}`, status {session.get('status')}, "
        f"{sum(calls.values())} calls, {sum(c for _, c in failures.values())} failed.\n"
    )
    print(f"Skills loaded, in order: {', '.join(f'`{s}`' for s in skills) or 'none'}.\n")
    print("Calls: " + ", ".join(f"`{name}` ×{count}" for name, count in calls.most_common()) + "\n")
    if failures:
        print("Failures (distinct):\n")
        for key, (text, count) in failures.items():
            print(f"- ×{count} `{key.split(':', 1)[0]}`: {text}")
        print()
    if replies:
        print("Final reply:\n")
        print(replies[-1][: args.reply_chars])


if __name__ == "__main__":
    main()
