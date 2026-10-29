"""Live study sources for views (Addendum B): ``study://<study>/<source>``.

A view's tables and charts name a study source as their ``dataUri`` and read it through
``POST /v1/studies/{study}/table-query``. Each read builds the source from what the
study already records, so a view shows new entries without being rebuilt:

* ``measurements``: one row per measured value (:func:`measurements.rows`), with the
  observation's status and time and the branch's title; when the study has a current
  law, the rows of its measured metric gain ``law_value`` and every row ``law``.
* ``entries``: one row per entry, with its type, branch, status, time and summary.
* ``records/<view>``: every plugin record row of that view name across the study's
  sessions (``_meta["ai.iowarp.clio/ui_event"]`` on tool results), the newest event
  per ``(group, key)`` winning.

Nothing is stored: the rows are read from the science log and the transcripts.
"""

from __future__ import annotations

import re
from typing import Any

from clio_agent.gact.science_log.entries import EntryRejected
from clio_agent.gact.science_log.formula import FormulaError, evaluate_formula, parse_formula

STUDY_URI = re.compile(
    r"^study://(?P<study>[A-Za-z0-9_-]+)/(?P<source>measurements|entries|records/(?P<view>[A-Za-z0-9_.-]+))$"
)
#: Hypothesis statuses that no longer stand as the study's law.
_SET_ASIDE = frozenset({"refuted", "superseded", "withdrawn"})
#: Field names that summarize an entry, in the order they are tried.
_SUMMARY_FIELDS = (
    "statement",
    "claim",
    "method",
    "result",
    "interpretation",
    "description",
    "title",
)


def parse_study_uri(uri: str) -> tuple[str, str, str]:
    """``(study, source, view)`` of a ``study://`` reference; ``view`` is empty unless
    the source is ``records``. Raises :class:`EntryRejected` (``study_uri_invalid``)."""

    match = STUDY_URI.match(str(uri or "").strip())
    if match is None:
        raise EntryRejected(
            "study_uri_invalid",
            "a study source is study://<study>/measurements, study://<study>/entries or "
            "study://<study>/records/<view>",
        )
    source = match.group("source")
    view = match.group("view") or ""
    return match.group("study"), ("records" if view else source), view


def _titles(app: Any, sessions: list[str]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for session_id in sessions:
        session = app.state.sessions.get(session_id)
        metadata = dict(getattr(session, "metadata", None) or {})
        titles[session_id] = str(
            metadata.get("subagent_name") or getattr(session, "title", "") or session_id
        )
    return titles


def _summary(entry: dict[str, Any]) -> str:
    fields = entry.get("fields") or {}
    for name in _SUMMARY_FIELDS:
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    claims = fields.get("claims")
    if isinstance(claims, list) and claims and isinstance(claims[0], dict):
        return str(claims[0].get("text") or "")
    return ""


def current_law(
    entries: list[dict[str, Any]], metrics: set[str], conditions: set[str]
) -> dict[str, Any] | None:
    """The study's current law: the newest hypothesis whose ``formula`` parses, whose
    status still stands, whose left side is a measured metric and whose right side uses
    only condition names. ``None`` when no hypothesis qualifies."""

    hypotheses = [e for e in entries if e.get("type") == "hypothesis"]
    for entry in sorted(hypotheses, key=lambda e: str(e.get("created_at")), reverse=True):
        if str(entry.get("status") or "").strip() in _SET_ASIDE:
            continue
        text = str((entry.get("fields") or {}).get("formula") or "")
        if not text.strip():
            continue
        try:
            formula = parse_formula(text)
        except FormulaError:
            continue
        if formula.target in metrics and set(formula.names) <= conditions:
            claim = _summary(entry)
            short = claim if len(claim) <= 60 else claim[:59] + "…"
            return {"formula": formula, "label": f"{entry['id']}: {short}: {text.strip()}"}
    return None


def _measurement_rows(
    app: Any, entries: list[dict[str, Any]], titles: dict[str, str]
) -> list[dict[str, Any]]:
    from clio_agent.gact.science_log.measurements import rows  # noqa: PLC0415

    observations = {e["id"]: e for e in entries if e.get("type") == "observation"}
    out = rows(list(observations.values()))
    for row in out:
        entry = observations.get(row["observation"], {})
        row["observation_status"] = entry.get("status", "")
        row["recorded_at"] = entry.get("created_at", "")
        row["branch_title"] = titles.get(row["branch"], row["branch"])
    metrics = {str(row["metric"]) for row in out}
    conditions = {
        key[len("condition.") :] for row in out for key in row if key.startswith("condition.")
    }
    law = current_law(entries, metrics, conditions)
    if law is not None:
        formula = law["formula"]
        for row in out:
            row["law"] = law["label"]
            if row["metric"] != formula.target:
                continue
            values: dict[str, float] = {}
            for name in formula.names:
                value = row.get(f"condition.{name}")
                if isinstance(value, bool) or not isinstance(value, int | float):
                    break
                values[name] = float(value)
            else:
                row["law_value"] = evaluate_formula(formula, values)
    return out


def _entry_rows(entries: list[dict[str, Any]], titles: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "id": entry["id"],
            "type": entry["type"],
            "branch": entry["branch"],
            "branch_title": titles.get(entry["branch"], entry["branch"]),
            "status": entry.get("status", ""),
            "created_at": entry.get("created_at", ""),
            "summary": _summary(entry),
        }
        for entry in entries
        if entry.get("type") not in {"status", "link"}
    ]


def _record_rows(
    app: Any, sessions: list[str], view: str, titles: dict[str, str]
) -> list[dict[str, Any]]:
    from clio_agent.gact.science_log.lineage import _parts  # noqa: PLC0415

    newest: dict[tuple[str, str], tuple[str, list[dict[str, Any]], str]] = {}
    for session_id in sessions:
        for part in _parts(app, session_id):
            metadata = getattr(part, "metadata", None) or {}
            event = metadata.get("ui_event") if isinstance(metadata, dict) else None
            if not isinstance(event, dict) or event.get("view") != view:
                continue
            item = (str(event.get("group") or session_id), str(event.get("key") or ""))
            at = str(metadata.get("completed_at") or "")
            rows = [row for row in event.get("rows") or [] if isinstance(row, dict)]
            if item not in newest or at >= newest[item][0]:
                newest[item] = (at, rows, session_id)
    out = []
    for (group, key), (at, rows, session_id) in newest.items():
        for row in rows:
            out.append(
                {
                    **row,
                    "group": group,
                    "key": key,
                    "session_id": session_id,
                    "branch": titles.get(session_id, session_id),
                    "recorded_at": at,
                }
            )
    return out


def source_rows(app: Any, study: str, source: str, view: str = "") -> list[dict[str, Any]]:
    """The rows of one study source, read now. Raises ``study_not_found``."""

    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    log = science_log(app)
    sessions = log.study_sessions(study)
    entries = log.effective(study)
    if not sessions or not any(entry["id"] == study for entry in entries):
        raise EntryRejected("study_not_found", f"no study {study!r} on this server")
    titles = _titles(app, sessions)
    if source == "measurements":
        return _measurement_rows(app, entries, titles)
    if source == "entries":
        return _entry_rows(entries, titles)
    return _record_rows(app, sessions, view, titles)


def rows_table(rows: list[dict[str, Any]]) -> Any:
    """The rows as an Arrow table: a column whose values mix numbers and text is text, so
    one table holds every row (a condition may be a number in one row and text in
    another)."""

    import pyarrow as pa  # noqa: PLC0415

    columns: list[str] = list(dict.fromkeys(key for row in rows for key in row))
    data: dict[str, list[Any]] = {}
    for name in columns:
        values = [row.get(name) for row in rows]
        present = [v for v in values if v is not None]
        numeric = all(isinstance(v, int | float) and not isinstance(v, bool) for v in present)
        flags = all(isinstance(v, bool) for v in present)
        if present and not numeric and not flags:
            values = [None if v is None else (v if isinstance(v, str) else str(v)) for v in values]
        elif present and numeric:
            values = [None if v is None else float(v) for v in values]
        data[name] = values
    return pa.table(data) if columns else pa.table({})


__all__ = ["STUDY_URI", "current_law", "parse_study_uri", "rows_table", "source_rows"]
