"""Mechanical grading against the manifest's answer keys. No LLM judge (TD23).

``uv run --frozen --no-sync python -m scripts.eval.tool_calling.grade RUN.json [--fixtures DIR]``

- P1: the definition's file:line, and the recall/precision of call sites (file:line).
- P2: the fact's line and value.
- P3: seeded issues matched by file and line range (precision/recall).
- P4: the share of checklist facts named (recorded, not in the plan's graded set).
- P5 to P7: the exact negative ids and their count.
- P8, P9: the expected tool called with the expected arguments.
- H1, H2 (and H3): the root-cause file and function are named.

Locations are read from the final answer: ``path.py:12``, ``path.py:12-15``,
``path.py line 12``, ``path.py (lines 12-15)``, ``path.py#L12``, and table
rows that hold a path and a line number in separate cells.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.paths import DEFAULT_FIXTURES, manifest_path

LINE_TOLERANCE = 1
P3_TOLERANCE = 3
_PATH = re.compile(r"[A-Za-z0-9_./\\-]*[A-Za-z0-9_]\.py\b")
_AFTER_PATH = re.compile(
    r"^[`'\")\]]*\s*(?:(?::|#L|,?\s*\(?\s*(?:lines?|L)\s*)(\d+)(?:\s*(?:-|–|to)\s*L?(\d+))?)",
    re.IGNORECASE,
)
_CELL_NUMBER = re.compile(r"^\s*`?L?(\d+)(?:\s*(?:-|–)\s*L?(\d+))?`?\s*$")
#: ":139" after a located path on the same line: another line of that file
#: ("`prompts.py:118`, `:139` and `:197`").
_MORE_LINES = re.compile(r"(?:(?<=\s)|(?<=,)|(?<=`))`?:(\d+)(?:\s*(?:-|–)\s*(\d+))?")


def locations(text: str) -> list[tuple[str, int, int]]:
    """``(path, first line, last line)`` mentions in an answer, in order, deduplicated."""
    found: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        paths = list(_PATH.finditer(line))
        hits_in_line = 0
        for index, match in enumerate(paths):
            after = _AFTER_PATH.match(line[match.end() :])
            if after:
                first = int(after.group(1))
                found.append((match.group(0), first, int(after.group(2) or first)))
                hits_in_line += 1
                end = paths[index + 1].start() if index + 1 < len(paths) else len(line)
                for more in _MORE_LINES.finditer(line, match.end() + after.end(), end):
                    first = int(more.group(1))
                    found.append((match.group(0), first, int(more.group(2) or first)))
        if paths and not hits_in_line and "|" in line:
            cells = line.split("|")
            for cell in cells:
                number = _CELL_NUMBER.match(cell)
                if number:
                    first = int(number.group(1))
                    found.append((paths[0].group(0), first, int(number.group(2) or first)))
    seen: set[tuple[str, int, int]] = set()
    return [x for x in found if not (x in seen or seen.add(x))]


def same_file(reported: str, expected: str) -> bool:
    """A reported path names ``expected`` (suffix match on path segments)."""
    a = reported.replace("\\", "/").lstrip("./").strip("`")
    b = expected.replace("\\", "/").lstrip("./")
    return a == b or b.endswith(f"/{a}") or a.endswith(f"/{b}")


def _hits(loc: tuple[str, int, int], file: str, start: int, end: int, tol: int) -> bool:
    path, first, last = loc
    return same_file(path, file) and first <= end + tol and last >= start - tol


def grade_p1(answer: dict[str, Any], text: str) -> dict[str, Any]:
    locs = locations(text)
    d = answer["definition"]
    def_ok = any(_hits(x, d["file"], d["line"], d["line"], LINE_TOLERANCE) for x in locs)
    callers = answer["callers"]
    found = [
        c
        for c in callers
        if any(_hits(x, c["file"], c["line"], c["line"], LINE_TOLERANCE) for x in locs)
    ]
    others = [x for x in locs if not _hits(x, d["file"], d["line"], d["line"], LINE_TOLERANCE)]
    true_pos = [
        x
        for x in others
        if any(_hits(x, c["file"], c["line"], c["line"], LINE_TOLERANCE) for c in callers)
    ]
    recall = len(found) / len(callers) if callers else 1.0
    return {
        "correct": def_ok and recall == 1.0,
        "definition_ok": def_ok,
        "caller_recall": round(recall, 3),
        "caller_precision": round(len(true_pos) / len(others), 3) if others else None,
        "reported_locations": len(locs),
    }


def grade_p2(answer: dict[str, Any], text: str) -> dict[str, Any]:
    line_ok = any(
        x[1] - LINE_TOLERANCE <= answer["line"] <= x[2] + LINE_TOLERANCE
        for x in locations(text)
        if same_file(x[0], answer["file"])
    ) or bool(re.search(rf"\bline\s+{answer['line']}\b", text, re.IGNORECASE))
    numbers = {int(n) for n in re.findall(r"(?<![\d.])(\d+)(?![\d.])", text)}
    value_ok = answer["value"] in numbers
    return {"correct": line_ok and value_ok, "line_ok": line_ok, "value_ok": value_ok}


def grade_p3(answer: dict[str, Any], text: str) -> dict[str, Any]:
    locs = locations(text)
    issues = answer["issues"]
    matched = [
        i["id"]
        for i in issues
        if any(_hits(x, i["file"], i["line_start"], i["line_end"], P3_TOLERANCE) for x in locs)
    ]
    useful = [
        x
        for x in locs
        if any(_hits(x, i["file"], i["line_start"], i["line_end"], P3_TOLERANCE) for i in issues)
    ]
    return {
        "correct": len(matched) == len(issues),
        "matched": matched,
        "recall": round(len(matched) / len(issues), 3) if issues else None,
        "precision": round(len(useful) / len(locs), 3) if locs else None,
        "reported_locations": len(locs),
    }


def grade_p4(answer: dict[str, Any], text: str) -> dict[str, Any]:
    hits, total, per = 0, 0, {}
    for name, sub in answer["subsystems"].items():
        got = sum(
            1
            for fact in sub["facts"]
            if any(re.search(rf"\b{re.escape(term)}\b", text) for term in fact["match_any"])
        )
        per[name] = f"{got}/{len(sub['facts'])}"
        hits, total = hits + got, total + len(sub["facts"])
    return {"correct": None, "fact_recall": round(hits / total, 3) if total else None, "by": per}


_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)]|\|)")
#: "REC-00001 to REC-00200": the bounds of what was read, not ids in the answer.
_ID_RANGE = re.compile(r"(REC-\d{5})\s*(?:to|through|-|–|—)\s*(REC-\d{5})")
#: "**REC-01401–01600:** 01420, 01425": a range label whose list carries no prefix.
_LABELLED_LIST = re.compile(
    r"(REC-\d{5})\s*[-–—]\s*(\d{5})\W*:\W*(\d{5}(?:\s*,\s*(?:and\s+)?\d{5})*)"
)
#: "REC-00003 (0.57)" or "REC-00108 at 0.78": an id shown with a positive reading is a
#: mention, not an answer.
_POSITIVE_READING = re.compile(r"(REC-\d{5})\s*(?:\(|\bat\b)\s*(?=\d)[\d.]+")
#: "REC-00002, 00005, 00017": a list that writes the prefix once; each number is an id.
_ID_LIST = re.compile(r"REC-\d{5}(?:\s*,\s*(?:and\s+)?\d{5}\b)+")


#: "00002, 00005, 00017" on its own line, after "each prefixed `REC-`": the prefix is
#: stated once for the whole list.
_BARE_ID_LIST = re.compile(r"^\s*\d{5}(?:\s*,\s*(?:and\s+)?\d{5})+\s*[.,;]?\s*$")
_PREFIX_STATED = re.compile(r"prefix(?:ed)?\W+(?:with\s+)?`?REC-`?|`?REC-`?\s+prefix", re.I)


def _expand_id_lists(line: str, *, prefix_stated: bool = False) -> str:
    if prefix_stated and _BARE_ID_LIST.match(line):
        return ", ".join(f"REC-{n}" for n in re.findall(r"\d{5}", line))
    return _ID_LIST.sub(
        lambda m: ", ".join(f"REC-{n}" for n in re.findall(r"\d{5}", m.group(0))), line
    )


def _claimed_ids(text: str) -> tuple[set[str], set[str]]:
    """``(claimed, mentioned)`` record ids: an id is part of the answer when it is in a list
    item or table row, on a line with a negative number, or listed with other ids; a lone
    id in a prose aside ("REC-00001 is in kPa") or the bound of a range ("REC-00001 to
    REC-00200") is only mentioned."""
    claimed: set[str] = set()
    mentioned: set[str] = set()
    prefix_stated = bool(_PREFIX_STATED.search(text))
    for line in text.splitlines():
        if labelled := _LABELLED_LIST.search(line):
            mentioned |= {labelled.group(1), f"REC-{labelled.group(2)}"}
            claimed |= {f"REC-{n}" for n in re.findall(r"\d{5}", labelled.group(3))}
            line = line[: labelled.start()] + line[labelled.end() :]
        for bounds in _ID_RANGE.findall(line):
            mentioned |= set(bounds)
        line = _ID_RANGE.sub("", line)
        for shown in _POSITIVE_READING.findall(line):
            mentioned.add(shown)
        line = _POSITIVE_READING.sub("", line)
        line = _expand_id_lists(line, prefix_stated=prefix_stated)
        ids = set(re.findall(r"REC-\d{5}", line))
        if not ids:
            continue
        if _LIST_LINE.match(line) or re.search(r"(?<![\w.])-\d", line) or len(ids) > 1:
            claimed |= ids
        else:
            mentioned |= ids
    return claimed, mentioned - claimed


def grade_records(answer: dict[str, Any], text: str) -> dict[str, Any]:
    ids, mentioned = _claimed_ids(text)
    expected = set(answer["ids"])
    numbers = {int(n) for n in re.findall(r"(?<![\d-])(\d+)(?![\d])", text.replace(",", ""))}
    count_ok = answer["count"] in numbers
    return {
        "correct": count_ok and ids == expected,
        "count_ok": count_ok,
        "ids_exact": ids == expected,
        "missing": sorted(expected - ids),
        "extra": sorted(ids - expected),
        "mentioned_only": sorted(mentioned - expected),
    }


def _arg_ok(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) < 1e-6
        except (TypeError, ValueError):
            return False
    a, e = str(actual or "").lower(), str(expected).lower()
    return a == e or e.split(".", 1)[-1] == a


def grade_tool_call(answer: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    calls = [c for s in run.get("sessions") or [] for c in s.get("tool_calls") or []]
    wanted = answer["tool"]
    named = [c for c in calls if str(c.get("tool") or "").endswith(wanted)]
    right = [
        c
        for c in named
        if all(_arg_ok((c.get("args") or {}).get(k), v) for k, v in answer["args"].items())
    ]
    return {
        "correct": bool(right),
        "tool_called": bool(named),
        "args_ok": bool(right),
        "calls_of_tool": len(named),
    }


def grade_root_cause(answer: dict[str, Any], text: str) -> dict[str, Any]:
    file_ok = any(Path(f).name in text or f in text for f in answer["root_cause_files"])
    function_ok = any(
        re.search(rf"\b{re.escape(f)}\b", text) for f in answer["root_cause_functions"]
    )
    commit = str(answer.get("introducing_commit") or "")
    commit_ok = bool(commit) and any(
        commit.startswith(token) for token in re.findall(r"\b[0-9a-f]{7,40}\b", text)
    )
    return {
        "correct": file_ok and function_ok,
        "file_ok": file_ok,
        "function_ok": function_ok,
        "commit_named": commit_ok,
    }


def grade(task: str, answer: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Grade one run; ``correct`` is ``None`` where the task has no mechanical key."""
    text = str((run.get("outcome") or {}).get("final_text") or "")
    if task == "P1":
        result = grade_p1(answer, text)
    elif task == "P2":
        result = grade_p2(answer, text)
    elif task == "P3":
        result = grade_p3(answer, text)
    elif task == "P4":
        result = grade_p4(answer, text)
    elif task in ("P5", "P6", "P7"):
        result = grade_records(answer, text)
    elif task in ("P8", "P9"):
        result = grade_tool_call(answer, run)
    elif task in ("H1", "H2", "H3"):
        result = grade_root_cause(answer, text)
    elif task == "P10":
        servers = tuple(answer.get("deferred_servers") or ())
        calls = [c for s in run.get("sessions") or [] for c in s.get("tool_calls") or []]
        result = {
            "correct": None,
            "deferred_tool_calls": sum(1 for c in calls if str(c.get("tool")).startswith(servers)),
        }
    else:
        result = {"correct": None}
    return {"task": task, "graded": result.get("correct") is not None, **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grade", description="Grade runs against answer keys.")
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)
    manifest = json.loads(manifest_path(args.fixtures).read_text(encoding="utf-8"))
    for path in args.runs:
        run = json.loads(path.read_text(encoding="utf-8"))
        answer = manifest["tasks"][run["task"]]["answer"]
        print(json.dumps({"run": str(path), **grade(run["task"], answer, run)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
