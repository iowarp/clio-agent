"""Report the facts of a study's record, without judging its numbers.

From a study snapshot (:mod:`study_snapshot`), this reports:

* the study: its question, its branches (how each relates to the others, its task
  status) and how many entries of each type each branch wrote;
* every Observation: the calls it cites and, for each, the session that recorded it
  and whether that session lies in the Observation's branch's task tree or in its
  forker's history (a fork's copied transcript); its measurements and cost;
* every Evidence and every Conclusion claim: whether it states a number, the analysis
  call it cites with that call's script hash, input file hashes, environment tier
  and the seed recorded for it;
* the decision tool (``--decision-tool``, e.g. ``guess``): how many calls each branch
  made, that each is a tool call in a model turn, and every shell command run in the
  study, verbatim, for a reader to check what code ran during play.

It computes no statistics and ranks nothing: rule 11 of the plan.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from study_snapshot import load

_NUMBER = re.compile(r"\d")


def _descendants(branches: list[dict[str, Any]]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = defaultdict(set)
    for row in branches:
        if row.get("parent_session_id"):
            children[row["parent_session_id"]].add(row["session_id"])

    def under(session: str) -> set[str]:
        out, stack = {session}, [session]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    return {row["session_id"]: under(row["session_id"]) for row in branches}


def _call_id(reference: str) -> str:
    return str(reference or "").rpartition("/")[2].removeprefix("call:")


def audit(data: dict[str, Any], decision_tool: str = "") -> dict[str, Any]:
    view = data.get("science_log") or {}
    branches = view.get("branches") or []
    entries = view.get("entries") or []
    by_id = {entry["id"]: entry for entry in entries}
    trees = _descendants(branches)
    rows = {row["session_id"]: row for row in branches}
    calls_by_session: dict[str, set[str]] = defaultdict(set)
    for call in data.get("calls") or []:
        calls_by_session[call["session"]].add(call["call_id"])
    transforms = data.get("transforms") or {}

    def forker(session: str) -> str:
        row = rows.get(session) or {}
        return str(row.get("fork_of_session_id") or "") if row.get("relation") == "fork" else ""

    observations = []
    for entry in entries:
        if entry["type"] != "observation":
            continue
        fields = entry.get("fields") or {}
        branch = entry["branch"]
        cited = []
        for row in fields.get("lineage") or []:
            holder = str(row.get("session") or "")
            source = forker(branch)
            cited.append(
                {
                    "call": row.get("reference") or row.get("call_id"),
                    "tool": row.get("tool"),
                    "recorded_in": holder,
                    "in_branch_tree": holder in trees.get(branch, {branch}),
                    "in_forker_history": bool(source)
                    and row.get("call_id") in calls_by_session.get(source, set()),
                    "result_sha256": row.get("result_sha256"),
                }
            )
        observations.append(
            {
                "id": entry["id"],
                "branch": branch,
                "cites": cited,
                "cites_calls": bool(cited),
                "measurements": len(fields.get("measurements") or []),
                "condition_keys": sorted(
                    {
                        k
                        for m in fields.get("measurements") or []
                        for k in (m.get("conditions") or {})
                    }
                ),
                "cost": fields.get("cost"),
                "artifacts": len(fields.get("artifacts") or []),
            }
        )

    def analysis(reference: str) -> dict[str, Any]:
        call_id = _call_id(reference)
        record = transforms.get(call_id) or {}
        instrument = record.get("instrument") or {}
        environment = record.get("environment") or {}
        seeds = sorted(
            {
                str((e.get("fields") or {}).get("seed"))
                for e in entries
                if e["type"] == "evidence"
                and _call_id((e.get("fields") or {}).get("analysis_call") or "") == call_id
                and (e.get("fields") or {}).get("seed") not in (None, "")
            }
        )
        return {
            "call": reference,
            "recorded": bool(record),
            "tool": instrument.get("tool") or record.get("tool"),
            "cmd": instrument.get("cmd"),
            "script_sha256": instrument.get("script_hash") or "",
            "inputs": [
                {"path": edge.get("path") or edge.get("uri"), "sha256": edge.get("sha256")}
                for edge in record.get("used") or []
            ],
            "environment_tier": environment.get("tier"),
            "seeds": seeds,
        }

    claims = []
    for entry in entries:
        fields = entry.get("fields") or {}
        if entry["type"] == "evidence":
            text = str(fields.get("interpretation") or "")
            claims.append(
                {
                    "entry": entry["id"],
                    "kind": "evidence",
                    "text": text,
                    "quantitative": bool(_NUMBER.search(text)),
                    "analysis": analysis(fields["analysis_call"])
                    if fields.get("analysis_call")
                    else None,
                }
            )
        if entry["type"] == "conclusion":
            for index, claim in enumerate(fields.get("claims") or []):
                text = str(claim.get("text") or "")
                claims.append(
                    {
                        "entry": entry["id"],
                        "kind": f"conclusion claim {index + 1}",
                        "text": text,
                        "cites": claim.get("cites") or [],
                        "cites_known": all(c in by_id for c in claim.get("cites") or []),
                        "quantitative": bool(_NUMBER.search(text)),
                        "analysis": analysis(claim["analysis_call"])
                        if claim.get("analysis_call")
                        else None,
                    }
                )

    decisions: dict[str, Any] = {}
    if decision_tool:
        made = [
            call
            for call in data.get("calls") or []
            if call["tool"] == decision_tool or call["tool"].endswith("_" + decision_tool)
        ]
        decisions = {
            "tool": decision_tool,
            "calls": len(made),
            "by_branch": dict(Counter(call["session"] for call in made)),
            "all_model_tool_calls": all(call.get("message_id") for call in made),
            "rejected": sum(bool(call.get("is_error")) for call in made),
            "shell_commands": [
                {
                    "session": call["session"],
                    "command": str((call.get("input") or {}).get("command") or "")[:2000],
                }
                for call in data.get("calls") or []
                if call["tool"] in {"shell_bash", "bash", "shell"}
                or call["tool"].startswith("shell_")
            ],
        }
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for entry in entries:
        counts[entry["branch"]][entry["type"]] += 1
    question = by_id.get(view.get("study") or "", {})
    return {
        "study": view.get("study"),
        "question": (question.get("fields") or {}).get("statement"),
        "branches": [
            {
                "session": row["session_id"],
                "title": row.get("title"),
                "relation": row.get("relation"),
                "parent": row.get("parent_session_id"),
                "fork_of": row.get("fork_of_session_id"),
                "task_status": row.get("task_status"),
                "node_folder": row.get("node_folder"),
                "entries": dict(counts.get(row["session_id"], {})),
            }
            for row in branches
        ],
        "observations": observations,
        "observations_citing_calls": sum(o["cites_calls"] for o in observations),
        "claims": claims,
        "quantitative_claims_citing_analysis": sum(
            bool(c["quantitative"] and c["analysis"]) for c in claims
        ),
        "quantitative_claims": sum(c["quantitative"] for c in claims),
        "decisions": decisions,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Study audit: {report.get('question')}",
        "",
        f"Study `{report.get('study')}`. Facts from the record; nothing here is a verdict.",
        "",
        "## Branches",
        "",
        "| Session | Title | Relation | Task | Entries |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["branches"]:
        lines.append(
            f"| `{row['session']}` | {row['title']} | {row['relation']} | "
            f"{row['task_status']} | {json.dumps(row['entries'])} |"
        )
    lines += [
        "",
        f"## Observations ({report['observations_citing_calls']} of "
        f"{len(report['observations'])} cite calls)",
        "",
        "| Entry | Branch | Cited calls | In branch tree | In forker history | Measurements |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for obs in report["observations"]:
        lines.append(
            f"| `{obs['id']}` | `{obs['branch']}` | {len(obs['cites'])} | "
            f"{sum(c['in_branch_tree'] for c in obs['cites'])} | "
            f"{sum(c['in_forker_history'] for c in obs['cites'])} | {obs['measurements']} |"
        )
    lines += [
        "",
        f"## Claims ({report['quantitative_claims_citing_analysis']} of "
        f"{report['quantitative_claims']} quantitative claims cite an analysis)",
        "",
    ]
    for claim in report["claims"]:
        analysis = claim.get("analysis") or {}
        lines.append(f"- `{claim['entry']}` {claim['kind']}: {claim['text'][:300]}")
        if analysis:
            lines.append(
                f"  - analysis `{analysis['call']}`: script `{analysis['script_sha256'][:12]}`, "
                f"inputs {[(i['path'], (i['sha256'] or '')[:12]) for i in analysis['inputs']]}, "
                f"environment `{analysis['environment_tier']}`, seeds {analysis['seeds']}"
            )
    decisions = report.get("decisions") or {}
    if decisions:
        lines += [
            "",
            f"## Decisions: `{decisions['tool']}`",
            "",
            f"{decisions['calls']} calls ({decisions['rejected']} rejected), by branch "
            f"{json.dumps(decisions['by_branch'])}; every one a tool call in a model turn: "
            f"{decisions['all_model_tool_calls']}.",
            "",
            f"Shell commands run during the study ({len(decisions['shell_commands'])}):",
            "",
        ]
        for row in decisions["shell_commands"]:
            lines.append(f"- `{row['session']}`: `{row['command'][:400]}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="a study snapshot JSON (study_snapshot.py)")
    parser.add_argument("--decision-tool", default="", help="the tool whose calls are decisions")
    parser.add_argument("--out", required=True, help="output folder")
    args = parser.parse_args()
    report = audit(load(args.snapshot), args.decision_tool)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(f"{out / 'audit.md'}")


if __name__ == "__main__":
    main()
