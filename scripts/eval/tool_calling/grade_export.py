"""H4 and H5 grading: the seeded findings of the ``h4-export`` investigation.

The answer is split into passages: a heading, a paragraph, a list item (with
the list item it is nested under) or a table row (with the table's header),
each carrying the heading above it. A finding counts when one passage both
names its run and says what is wrong in the answer key's terms:

- a run is named by its id (``run_0112``, or ``0112`` alone), or by its
  chamber together with a date it ran on (``2026-03-16``, ``16 March``,
  ``Mar 16``, ``3/16``) or its campaign (``campaign 3``);
- ``calibration``, ``unit``, ``duplicate`` and ``clock`` need one of their
  ``terms``, so naming the right run with the wrong problem does not count;
- the effect needs a passage about growth under far-red (or condition B) that
  gives a condition A and a condition B value of one of the key's growth
  metrics, B above A (a table counts as one passage here), or a relative gain
  or ratio close to the key's.

The decoy is flagged when a passage names its run and its dark interval with a
fault term and nothing saying the interval was planned. ``correct`` needs all
five findings and no flagged decoy. The index discrepancies and the measured
variables are recorded, not graded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any

#: A value matches a metric's condition median within this fraction (and within half
#: the A-B difference, so an A value cannot pass for a B value).
VALUE_TOLERANCE = 0.15
#: A relative gain ("41 % faster") matches within this many percentage points.
PERCENT_TOLERANCE = 10.0
#: Relative gains under this are too small to tell from other percentages in the text.
MIN_RELATIVE = 0.3

_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*\*[^*]+\*\*:?\s*$)")
_LIST = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s")
_TABLE = re.compile(r"^\s*\|")
_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\d])")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_RATIO = re.compile(r"(\d+(?:\.\d+)?)\s*(?:×|x\b|times\b|-fold)", re.IGNORECASE)
_GROWTH = re.compile(r"elongat|height|taller|\bstem|growth|\bgr[eo]w", re.IGNORECASE)
_FAR_RED = re.compile(r"(?i:far[- ]?red|condition b|\bcondB\b)|\bB\b|735")


def passages(text: str, *, join_tables: bool = False) -> list[str]:
    """The answer's passages, each prefixed by its heading and parent list item."""
    out: list[str] = []
    heading = parent = header = ""
    current: list[str] = []
    in_table = False

    def flush() -> None:
        if any(current):
            out.append("\n".join(part for part in current if part))
        current.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            parent, in_table = "", False
            continue
        if _HEADING.match(line):
            flush()
            heading, parent, in_table = line, "", False
            out.append(line)
            continue
        if _TABLE.match(line):
            if _RULE.match(line):
                continue
            if not in_table:
                flush()
                header, in_table = line, True
                current.extend([heading, line])
            elif join_tables:
                current.append(line)
            else:
                flush()
                current.extend([heading, header, line])
            continue
        in_table = False
        item = _LIST.match(line)
        if item:
            flush()
            nested = len(item.group(1)) >= 2 and parent
            current.extend([heading, parent if nested else "", line])
            if not nested:
                parent = line
            continue
        if not current:
            current.extend([heading, line])
        else:
            current.append(line)
    flush()
    return out


def _any(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _date_patterns(iso_day: str) -> list[str]:
    day = date.fromisoformat(iso_day)
    month = day.strftime("%B")
    names = rf"(?:{month}|{month[:3]})\.?"
    d, m = day.day, day.month
    return [
        re.escape(iso_day),
        rf"(?<!\d){m:02d}-{d:02d}(?!\d)",
        rf"(?<!\d){d}(?:st|nd|rd|th)?\s+{names}(?![a-z])",
        rf"(?<![a-z]){names}\s+{d}(?:st|nd|rd|th)?(?!\d)",
        rf"(?<![\d/]){m}/{d}(?![\d/])",
        rf"(?<![\d/]){d}/{m}(?![\d/])",
    ]


def names_run(where: dict[str, Any], text: str) -> bool:
    """Whether ``text`` names the run ``where`` describes (see the module docstring)."""
    for rid in where.get("runs") or []:
        number = rid.split("_")[-1]
        if re.search(rf"run[_\s-]?0*{int(number)}(?!\d)|(?<![\d.]){number}(?![\d.])", text, re.I):
            return True
    chamber = where.get("chamber")
    if not chamber:
        return False
    digit = chamber[-1]
    if not re.search(rf"\bGC[-\s]?{digit}\b|\bchamber\s+{digit}\b", text, re.IGNORECASE):
        return False
    campaign = where.get("campaign")
    if campaign and re.search(
        rf"\bcampaign\s+C?{campaign}\b|\bC{campaign}\s+campaign", text, re.IGNORECASE
    ):
        return True
    return any(_any(_date_patterns(day), text) for day in where.get("dates") or [])


def _finding(where: dict[str, Any], terms: list[str], chunks: list[str]) -> bool:
    return any(names_run(where, c) and _any(terms, c) for c in chunks)


def _numbers(text: str) -> list[float]:
    return [float(n) for n in _NUMBER.findall(text.replace(",", ""))]


def _metric_values(metric: dict[str, Any], numbers: list[float]) -> bool:
    a, b = float(metric["A"]), float(metric["B"])
    tol_a = min(VALUE_TOLERANCE * abs(a), abs(b - a) / 2)
    tol_b = min(VALUE_TOLERANCE * abs(b), abs(b - a) / 2)
    near_a = [x for x in numbers if abs(x - a) <= tol_a]
    near_b = [y for y in numbers if abs(y - b) <= tol_b]
    return any(y > x for x in near_a for y in near_b)


def _metric_relative(metric: dict[str, Any], chunk: str) -> bool:
    relative = float(metric["relative"])
    if relative < MIN_RELATIVE:
        return False
    if any(abs(float(p) - 100 * relative) <= PERCENT_TOLERANCE for p in _PERCENT.findall(chunk)):
        return True
    return any(abs(float(r) - (1 + relative)) <= 0.1 for r in _RATIO.findall(chunk))


def effect_found(effect: dict[str, Any], chunks: list[str]) -> bool:
    """A passage on growth under far-red gives the effect's size, B above A."""
    for chunk in chunks:
        if not (_GROWTH.search(chunk) and _FAR_RED.search(chunk)):
            continue
        numbers = _numbers(chunk)
        for metric in effect["metrics"]:
            if _metric_values(metric, numbers) or _metric_relative(metric, chunk):
                return True
    return False


def _decoy(decoy: dict[str, Any], chunks: list[str]) -> tuple[bool, bool]:
    """``(flagged as a fault, mentioned as planned)``.

    Flagged only when no passage about the interval says it was planned: an answer that
    explains the interval in one list item and notes a consequence with a fault word
    ("96 imaging frames are missing") in a sibling item has not called it a fault
    (H4 head1 run 3, 2026-11-03).
    """
    faulted = explained = False
    for chunk in chunks:
        if not (names_run(decoy, chunk) and _any(decoy["interval"], chunk)):
            continue
        if _any(decoy["explained"], chunk):
            explained = True
        elif _any(decoy["fault"], chunk):
            faulted = True
    return faulted and not explained, explained


def grade_export(answer: dict[str, Any], text: str) -> dict[str, Any]:
    """Per-finding found/not found for H4 and H5."""
    rows = passages(text)
    blocks = passages(text, join_tables=True)
    findings = {a["id"]: _finding(a, a["terms"], rows) for a in answer["anomalies"]}
    findings["effect"] = effect_found(answer["effect"], blocks)
    flagged, explained = _decoy(answer["decoy"], rows)
    index = answer.get("index") or {}
    index_terms = index.get("terms") or []
    measured = answer.get("measured") or []
    named = sum(
        1
        for fact in measured
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in fact["match_any"])
    )
    found = sum(findings.values())
    return {
        "correct": found == len(findings) and not flagged,
        "findings": findings,
        "finding_recall": round(found / len(findings), 3),
        "decoy_flagged": flagged,
        "decoy_explained": explained,
        "index": {
            kind: _finding(index[kind], index_terms, rows)
            for kind in ("phantom", "unlisted")
            if kind in index
        },
        "measured_recall": round(named / len(measured), 3) if measured else None,
    }


_MM = re.compile(r"\d\s*mm\b", re.IGNORECASE)
_CM = re.compile(r"\d\s*cm\b", re.IGNORECASE)
_HEIGHT = re.compile(r"\b(?:heights?|elongation|taller|tall)\b", re.IGNORECASE)
#: Campaign the H6 request first excludes and the change then includes.
H6_CHANGED_CAMPAIGN = 3


def _problem_table(text: str, min_rows: int) -> bool:
    """A Markdown table whose header names run, problem and evidence, with enough rows."""
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        head = line.lower()
        if line.startswith("|") and all(w in head for w in ("run", "problem", "evidence")):
            rows = 0
            for row in lines[i + 2 :]:
                if not row.startswith("|"):
                    break
                rows += 1
            if rows >= min_rows:
                return True
    return False


def grade_h6(answer: dict[str, Any], text: str) -> dict[str, Any]:
    """H6: H4's findings, plus the person's constraints and the change made mid-run.

    - ``heights_in_cm``: a passage about height gives a value in cm, and none in mm;
    - ``change_honoured``: every problem in campaign C3, first excluded and then
      included by the change, is reported;
    - ``problem_table``: a table with run, problem and evidence columns and a row per
      problem.
    ``correct`` needs H4's findings and every constraint.
    """
    result = grade_export(answer, text)
    heights = [r for r in passages(text) if _HEIGHT.search(r)]
    changed = [a["id"] for a in answer["anomalies"] if int(a["campaign"]) == H6_CHANGED_CAMPAIGN]
    constraints = {
        "heights_in_cm": any(_CM.search(r) for r in heights)
        and not any(_MM.search(r) for r in heights),
        "change_honoured": all(result["findings"][kind] for kind in changed),
        "problem_table": _problem_table(text, len(answer["anomalies"])),
    }
    return {
        **result,
        "constraints": constraints,
        "correct": result["correct"] and all(constraints.values()),
    }


_BLAME = re.compile(r"\b(?:caused|cause of|due to|because of|responsible|root cause)\b", re.I)
_CLEARED = re.compile(
    r"\b(?:within|not (?:the )?cause|ruled out|no samples affected|unrelated|normal)\b", re.I
)


def grade_trail(answer: dict[str, Any], text: str) -> dict[str, Any]:
    """H7: the cause is the recalled lamp lot (its drift lowers the 340 nm readings).

    ``correct`` needs a passage naming the lot (or the notice) with a cause term, and no
    passage blaming a distractor (the freezer alarm, the pipette) without clearing it.
    The chain's hops named anywhere are recorded, not graded.
    """
    rows = passages(text)
    cause = any(
        (answer["lot"] in r or answer["notice"] in r)
        and any(t in r.lower() for t in answer["cause_terms"])
        for r in rows
    )
    blamed = sorted(
        name
        for name, terms in answer["distractors"].items()
        if any(
            any(t.lower() in r.lower() for t in terms)
            and _BLAME.search(r)
            and not _CLEARED.search(r)
            for r in rows
        )
    )
    hops = {
        "instrument": answer["instrument"] in text,
        "service_day": answer["service_day"] in text,
        "lot": answer["lot"] in text,
        "notice": answer["notice"] in text,
    }
    return {
        "correct": cause and not blamed,
        "cause_found": cause,
        "distractors_blamed": blamed,
        "hops": hops,
    }


def grade_breadth(answer: dict[str, Any], text: str) -> dict[str, Any]:
    """H8: each failed batch's cause, found or not.

    A batch counts when one passage names it together with its cause: one of its
    evidence ids (a lot, a notice, a note, the swapped batch) or a term for its cause's
    kind. ``correct`` needs every batch; ``batch_recall`` is the share found.
    """
    rows = passages(text, join_tables=True)
    found: dict[str, bool] = {}
    for entry in answer["batches"]:
        batch, terms = entry["batch"], answer["cause_terms"][entry["kind"]]
        found[batch] = any(
            re.search(rf"\b{re.escape(batch)}\b", r)
            and (any(i in r for i in entry["ids"]) or any(t in r.lower() for t in terms))
            for r in rows
        )
    recall = sum(found.values()) / len(found) if found else 0.0
    return {"correct": all(found.values()), "batch_recall": round(recall, 3), "found": found}
