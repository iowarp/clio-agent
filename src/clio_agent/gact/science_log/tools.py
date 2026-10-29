"""The science log's tools, claimed by the ``science-log`` plugin.

``log_entry`` writes a finding, ``link`` adds a relation later, ``set_status``
records a status change, ``query_log`` reads the study (measurements as rows), and
``export_measurements`` writes measurements to a data file for the agent's own
analysis. A refusal is a typed result (``accepted: false`` with a ``reason``); the
log never judges whether a claim is right.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from clio_agent.gact.science_log.entries import ENTRY_TYPES, EntryRejected, type_signatures

#: Every tool name the ``science-log`` plugin may claim.
SCIENCE_LOG_TOOLS: tuple[str, ...] = (
    "log_entry",
    "link",
    "set_status",
    "query_log",
    "export_measurements",
)


def _context() -> tuple[Any, str, str]:
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.artifacts.observer_bridge import current_call_id  # noqa: PLC0415
    from clio_agent.tools.task_call_context import TASK_CALL  # noqa: PLC0415

    app, session_id = context.active_app(), context.active_session_id()
    if app is None or not session_id:
        raise RuntimeError("the science log needs an active session")
    # The call that writes the entry: its id keys the entry, so a replay writes nothing
    # twice, and it locates the turn that wrote it.
    return app, session_id, current_call_id() or TASK_CALL.get().invocation_id or uuid4().hex


def _native(func: Any, fields: str, *, read_only: bool = False, **declaration: Any) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(
        func,
        presentation=f"fields:{fields}",
        domain="science",
        read_only=read_only,
        **declaration,
    )


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "accepted": True,
        "id": entry["id"],
        "type": entry["type"],
        "study": entry["study"],
        "branch": entry["branch"],
        "status": entry["status"],
    }


def log_entry(type: str, fields: dict[str, Any], links: Any = None) -> dict[str, Any]:  # noqa: A002
    """Write one entry of the study log."""

    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    app, session_id, invocation = _context()
    try:
        return _summary(science_log(app).write(session_id, type, fields, links, invocation))
    except EntryRejected as refused:
        return refused.to_dict()


def link(from_id: str, relation: str, to_id: str) -> dict[str, Any]:
    """Add a relation between two entries of the study."""

    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    app, session_id, invocation = _context()
    try:
        return _summary(science_log(app).link(session_id, from_id, relation, to_id, invocation))
    except EntryRejected as refused:
        return refused.to_dict()


def set_status(entry_id: str, status: str, reason: str) -> dict[str, Any]:
    """Record a status change of an entry, with its reason."""

    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    app, session_id, invocation = _context()
    try:
        return _summary(
            science_log(app).set_status(session_id, entry_id, status, reason, invocation)
        )
    except EntryRejected as refused:
        return refused.to_dict()


def _select(
    app: Any, session_id: str, filter: dict[str, Any] | None
) -> tuple[str, list[dict[str, Any]]]:  # noqa: A002
    from clio_agent.gact.science_log.store import science_log  # noqa: PLC0415

    log = science_log(app)
    study = log.study_of(session_id)
    if study is None:
        raise EntryRejected("study_missing", "this session is not in a study")
    wanted = dict(filter or {})
    entries = log.effective(study)

    def keep(entry: dict[str, Any]) -> bool:
        if wanted.get("type") and entry["type"] not in _many(wanted["type"]):
            return False
        if wanted.get("branch") and entry["branch"] not in _many(wanted["branch"]):
            return False
        if wanted.get("status") and entry["status"] not in _many(wanted["status"]):
            return False
        if wanted.get("ids") and entry["id"] not in _many(wanted["ids"]):
            return False
        if wanted.get("links_to") and not any(
            row["to"] in _many(wanted["links_to"])
            and (not wanted.get("relation") or row["relation"] == wanted["relation"])
            for row in entry["links"]
        ):
            return False
        text = str(wanted.get("text") or "").lower()
        return not text or text in str(entry["fields"]).lower()

    selected = [entry for entry in entries if keep(entry)]
    limit = int(wanted.get("limit") or 0)
    return study, selected[-limit:] if limit > 0 else selected


def _many(value: Any) -> set[str]:
    return {str(v) for v in value} if isinstance(value, list) else {str(value)}


def query_log(filter: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: A002
    """Read the study's entries, filtered; Observations' measurements also as rows."""

    from clio_agent.gact.science_log.measurements import rows  # noqa: PLC0415

    app, session_id, _invocation = _context()
    try:
        study, selected = _select(app, session_id, filter)
    except EntryRejected as refused:
        return refused.to_dict()
    return {
        "study": study,
        "branch": session_id,
        "entries": selected,
        "measurements": rows(selected),
    }


def export_measurements(
    filter: dict[str, Any] | None = None, format: str = "parquet"
) -> dict[str, Any]:  # noqa: A002
    """Write the matching Observations' measurements to a data file with a manifest."""

    from clio_agent.gact.science_log.measurements import (  # noqa: PLC0415
        export_folder,
        write_export,
    )

    app, session_id, invocation = _context()
    try:
        study, selected = _select(app, session_id, {**(filter or {}), "type": "observation"})
        stem = "measurements-" + invocation.replace("/", "_")[-24:]
        written = write_export(
            export_folder(app, session_id), selected, dict(filter or {}), format, stem
        )
    except EntryRejected as refused:
        return refused.to_dict()
    return {"accepted": True, "study": study, **written}


_TYPES = ", ".join(ENTRY_TYPES)


def build_log_entry_tool() -> Any:
    return _native(
        log_entry,
        "type,id,status,reason",
        name="log_entry",
        title="Log entry",
        desc=(
            f"Write one entry of the study log: one of {_TYPES}. fields hold the type's "
            "fields; links name the entries it relates to ({relation: id}). Per type: "
            f"{type_signatures()}. A measurement is {{metric, value?, source?: {{call, field}}, "
            "unit?, conditions?}} (source.field is a path into the call's result, such as "
            "readings.0.value); a conclusion claim is {{text, cites, analysis_call?}}. An Observation "
            "cites the recorded calls behind it in from_calls (the call: reference each tool "
            "result carries); a Conclusion's claims each cite entries. A view attaches a surface "
            "this conversation made (surface_id) to the study as one of its dashboards; its "
            "charts and tables can read study://<study>/measurements, /entries or "
            "/records/<name>, which stay current as entries arrive. Returns the entry id; "
            "a refusal names its reason."
        ),
        args={
            "type": {"type": "string", "enum": list(ENTRY_TYPES)},
            "fields": {"type": "object"},
            "links": {
                "anyOf": [{"type": "object"}, {"type": "array"}, {"type": "null"}],
                "default": None,
            },
        },
    )


def build_link_tool() -> Any:
    return _native(
        link,
        "id,type,reason",
        name="link",
        title="Link entries",
        desc="Add a relation between two entries of the study, such as evidence that "
        "supports another branch's hypothesis.",
        args={
            "from_id": {"type": "string"},
            "relation": {
                "type": "string",
                "enum": [
                    "supports",
                    "refutes",
                    "relates_to",
                    "cites",
                    "derived_from",
                    "answers",
                    "merges",
                ],
            },
            "to_id": {"type": "string"},
        },
    )


def build_set_status_tool() -> Any:
    return _native(
        set_status,
        "id,status,reason",
        name="set_status",
        title="Set entry status",
        desc="Record a status change of an entry (for a hypothesis: supported, refuted or "
        "superseded), with the reason. The entry itself is never edited.",
        args={
            "entry_id": {"type": "string"},
            "status": {"type": "string"},
            "reason": {"type": "string"},
        },
    )


def build_query_log_tool() -> Any:
    return _native(
        query_log,
        "study,branch",
        name="query_log",
        title="Query study log",
        desc=(
            "Read the study's entries, every branch's, with sources and current status. "
            "filter may hold type, branch, status, ids, links_to (with relation), text and "
            "limit. Observations' measurements also come back as rows."
        ),
        args={"filter": {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None}},
        read_only=True,
    )


def build_export_measurements_tool() -> Any:
    return _native(
        export_measurements,
        "output_path,saved_path,reason",
        name="export_measurements",
        title="Export measurements",
        desc=(
            "Write the measurements of the Observations matching filter to a data file "
            "(parquet, or csv) next to a manifest with the Observation ids, condition keys "
            "and the file's SHA-256, for your own analysis code."
        ),
        args={
            "filter": {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None},
            "format": {"type": "string", "enum": ["parquet", "csv"], "default": "parquet"},
        },
    )


BUILDERS = {
    "log_entry": build_log_entry_tool,
    "link": build_link_tool,
    "set_status": build_set_status_tool,
    "query_log": build_query_log_tool,
    "export_measurements": build_export_measurements_tool,
}

__all__ = ["BUILDERS", "SCIENCE_LOG_TOOLS"]
