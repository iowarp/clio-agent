"""Writing and reading the science log on the task layer's one writer.

An entry is a ``science.entry`` semantic event in the event log of the session that
wrote it (its branch), recorded by ``TaskHistory`` with the confirmed-write path task
observations use. A study belongs to the session that wrote its root Question; its
id is that Question's id. Which sessions hold a study's entries is found through two
derived ``task_lookup`` list rows (``sci.s.<session>`` and ``sci.study.<study>``),
written before the event they describe, so a crash can only leave a hint the next
read re-checks. Entries load on demand from each session's log; nothing is replayed
at boot.

Every branch reads the whole study (siblings and cousins included) and writes only
its own entries. A session's study is the study of the first entry it wrote, else the
study its task tree's root belongs to, else (a person's branch of a conversation in a
study) the study of the conversation it was forked from.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from clio_agent.gact.science_log.entries import (
    DEFAULT_STATUS,
    LATER_RELATIONS,
    LINKS,
    STATUSES,
    EntryRejected,
    normalize_links,
    validate_fields,
)

logger = logging.getLogger(__name__)

EVENT_TYPE = "science.entry"
_SESSION_ROW = "sci.s."
_STUDY_ROW = "sci.study."


def entry_id(session_id: str, invocation: str) -> str:
    """A replay of the same call writes the same entry, never a second one."""

    return "e_" + hashlib.sha256((session_id + "\0" + invocation).encode()).hexdigest()[:12]


class ScienceLog:
    """The study log for one app: reads per session on demand, writes through history."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.history = app.state.task_history
        self._entries: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- reading
    def session_entries(self, session_id: str) -> list[dict[str, Any]]:
        """The entries ``session_id`` wrote, in order (loaded from its event log once)."""

        with self._lock:
            cached = self._entries.get(session_id)
            if cached is not None:
                return list(cached)
        rows: list[dict[str, Any]] = []
        live = getattr(self.history.arc, "_live", None)
        for segment in live.iter_session_event_segments(session_id) if live is not None else ():
            content = segment.content
            if str(content.get("event_type", "")) != EVENT_TYPE:
                continue
            record = (content.get("payload") or {}).get("record")
            if isinstance(record, dict) and not any(r["id"] == record.get("id") for r in rows):
                rows.append(record)
        with self._lock:
            self._entries.setdefault(session_id, rows)
            return list(self._entries[session_id])

    def study_of(self, session_id: str, *, _seen: frozenset[str] = frozenset()) -> str | None:
        """The study ``session_id`` belongs to, or ``None``."""

        if not session_id or session_id in _seen:
            return None
        own = self.session_entries(session_id)
        if own:
            return str(own[0]["study"])
        index = self.history.index
        rows = index.rows(_SESSION_ROW, session_id)
        if rows:
            return str(rows[0])
        root = index.session_root(session_id)
        if root != session_id:
            found = self.study_of(root, _seen=_seen | {session_id})
            if found:
                return found
        session = self.app.state.sessions.get(session_id)
        origin = ((getattr(session, "metadata", None) or {}).get("fork_origin") or {}).get(
            "session_id"
        )
        return self.study_of(str(origin), _seen=_seen | {session_id}) if origin else None

    def study_sessions(self, study: str) -> list[str]:
        """The sessions holding a study's entries (its branches), root first."""

        rows = [str(row) for row in self.history.index.rows(_STUDY_ROW, study)]
        return list(dict.fromkeys(rows))

    def study_entries(self, study: str) -> list[dict[str, Any]]:
        """Every entry of the study, in the order each branch wrote them."""

        entries = [
            entry
            for session_id in self.study_sessions(study)
            for entry in self.session_entries(session_id)
            if entry.get("study") == study
        ]
        return sorted(entries, key=lambda e: (str(e.get("created_at")), str(e.get("id"))))

    def effective(self, study: str) -> list[dict[str, Any]]:
        """The study's findings with status changes and later links applied."""

        entries = self.study_entries(study)
        status: dict[str, dict[str, Any]] = {}
        later: dict[str, list[dict[str, str]]] = {}
        for entry in entries:
            if entry["type"] == "status":
                status[entry["fields"]["entry"]] = entry
            elif entry["type"] == "link":
                fields = entry["fields"]
                later.setdefault(fields["from"], []).append(
                    {"relation": fields["relation"], "to": fields["to"], "by": entry["id"]}
                )
        out = []
        for entry in entries:
            if entry["type"] in ("status", "link"):
                continue
            change = status.get(entry["id"])
            out.append(
                {
                    **entry,
                    "status": change["fields"]["status"] if change else entry["status"],
                    **({"status_reason": change["fields"]["reason"]} if change else {}),
                    "links": [*entry["links"], *later.get(entry["id"], [])],
                }
            )
        return out

    # ------------------------------------------------------------- writing
    def write(
        self,
        session_id: str,
        entry_type: str,
        fields: Any,
        links: Any,
        invocation: str,
    ) -> dict[str, Any]:
        """Validate and record one finding; returns it, or raises :class:`EntryRejected`."""

        from clio_agent.gact.science_log.lineage import observation_sources  # noqa: PLC0415

        clean = validate_fields(entry_type, fields)
        rows = normalize_links(entry_type, links)
        identity = entry_id(session_id, invocation)
        with self.history.serialized(writing=True):
            existing = self._existing(session_id, identity)
            if existing is not None:
                return existing
            study = self.study_of(session_id)
            if study is None:
                if entry_type != "question" or rows:
                    raise EntryRejected(
                        "study_missing",
                        "write the study's question first: this session is not in a study",
                    )
                study = identity  # the root Question names the study
            elif clean.get("budget") is not None:
                raise EntryRejected(
                    "budget_not_root", "only a study's root question declares a budget"
                )
            visible = {entry["id"]: entry for entry in self.study_entries(study)}
            self._check_links(entry_type, rows, visible)
            if entry_type == "conclusion":
                for claim in clean["claims"]:
                    self._check_cited(claim["cites"], visible)
            if entry_type == "observation":
                clean = observation_sources(self.app, session_id, clean)
                self._charge(study, visible, clean)
            if entry_type == "view":
                self._check_view_surface(session_id, clean["surface_id"])
            entry = {
                "id": identity,
                "study": study,
                "branch": session_id,
                "session": session_id,
                "type": entry_type,
                "fields": clean,
                "links": rows,
                "status": DEFAULT_STATUS[entry_type],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "invocation": invocation,
            }
            self._record(session_id, study, entry)
            return entry

    def link(
        self, session_id: str, source: str, relation: str, target: str, invocation: str
    ) -> dict[str, Any]:
        """Add a relation between two visible entries later (a ``link`` entry)."""

        if relation not in LATER_RELATIONS:
            raise EntryRejected(
                "link_relation_invalid",
                f"relation must be one of {', '.join(sorted(LATER_RELATIONS))}",
            )
        return self._meta(
            session_id,
            "link",
            {"from": source, "relation": relation, "to": target},
            (source, target),
            invocation,
        )

    def set_status(
        self, session_id: str, target: str, status: str, reason: str, invocation: str
    ) -> dict[str, Any]:
        """Record a status change of a visible entry (a ``status`` entry)."""

        if not str(reason or "").strip():
            raise EntryRejected("status_reason_missing", "a status change gives its reason")
        return self._meta(
            session_id,
            "status",
            {"entry": target, "status": status, "reason": reason},
            (target,),
            invocation,
        )

    # ------------------------------------------------------------- internals
    def _check_view_surface(self, session_id: str, surface_id: str) -> None:
        """A view attaches a surface of the writing session: only its owner edits it."""

        store = getattr(getattr(self.app, "state", None), "a2ui_store", None)
        record = store.get(session_id, surface_id) if store is not None else None
        if record is None or getattr(record, "state", "") == "deleted":
            raise EntryRejected(
                "view_surface_missing",
                f"this session has no surface {surface_id!r}: create it with "
                "create_a2ui_surface first; a view attaches a surface of the session "
                "that writes it",
            )

    def _meta(
        self,
        session_id: str,
        kind: str,
        fields: dict[str, Any],
        targets: Iterable[str],
        invocation: str,
    ) -> dict[str, Any]:
        identity = entry_id(session_id, invocation)
        with self.history.serialized(writing=True):
            existing = self._existing(session_id, identity)
            if existing is not None:
                return existing
            study = self.study_of(session_id)
            if study is None:
                raise EntryRejected("study_missing", "this session is not in a study")
            visible = {entry["id"]: entry for entry in self.study_entries(study)}
            for target in targets:
                if target not in visible:
                    raise EntryRejected(
                        "entry_unknown", f"{target!r} is not an entry of this study"
                    )
            if kind == "status":
                allowed = STATUSES[visible[fields["entry"]]["type"]]
                if fields["status"] not in allowed:
                    raise EntryRejected(
                        "status_invalid",
                        f"a {visible[fields['entry']]['type']} is one of "
                        f"{', '.join(sorted(allowed))}",
                    )
            entry = {
                "id": identity,
                "study": study,
                "branch": session_id,
                "session": session_id,
                "type": kind,
                "fields": fields,
                "links": [],
                "status": "recorded",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "invocation": invocation,
            }
            self._record(session_id, study, entry)
            return entry

    def _existing(self, session_id: str, identity: str) -> dict[str, Any] | None:
        return next((e for e in self.session_entries(session_id) if e["id"] == identity), None)

    @staticmethod
    def _check_links(
        entry_type: str, rows: list[dict[str, str]], visible: dict[str, dict[str, Any]]
    ) -> None:
        for row in rows:
            target = visible.get(row["to"])
            if target is None:
                raise EntryRejected(
                    "link_target_unknown", f"{row['to']!r} is not an entry of this study"
                )
            types = LINKS[entry_type][row["relation"]][0]
            if target["type"] not in types:
                raise EntryRejected(
                    "link_target_type",
                    f"{row['relation']!r} links to a {' or '.join(sorted(types))}, "
                    f"not a {target['type']}",
                )

    @staticmethod
    def _check_cited(cites: list[str], visible: dict[str, dict[str, Any]]) -> None:
        for cited in cites:
            if cited not in visible:
                raise EntryRejected(
                    "citation_unknown", f"claim cites {cited!r}, which is not an entry here"
                )

    @staticmethod
    def _charge(study: str, visible: dict[str, dict[str, Any]], fields: dict[str, Any]) -> None:
        root = visible.get(study)
        budget = (root or {}).get("fields", {}).get("budget") if root else None
        if not budget:
            return
        cost = fields.get("cost")
        if cost is None:
            raise EntryRejected(
                "cost_missing",
                f"this study has a budget of {budget['amount']} {budget['unit']}: "
                "an observation states its cost",
            )
        spent = sum(
            float(entry["fields"].get("cost") or 0)
            for entry in visible.values()
            if entry["type"] == "observation"
        )
        if spent + float(cost) > float(budget["amount"]):
            raise EntryRejected(
                "budget_exhausted",
                f"the study's budget of {budget['amount']} {budget['unit']} has "
                f"{float(budget['amount']) - spent:g} left; this observation costs {cost}",
            )

    def _record(self, session_id: str, study: str, entry: dict[str, Any]) -> None:
        """Index hints first, then the event, then this process's cache."""

        from clio_agent.gact.task_index_records import list_row  # noqa: PLC0415

        index = self.history.index
        # The writer and its task tree's root both name the study, so every session
        # of the tree finds it, whichever branch wrote first.
        for member in dict.fromkeys((session_id, index.session_root(session_id))):
            if not index.rows(_SESSION_ROW, member):
                index.put_lookup(list_row(_SESSION_ROW, member, [study]))
        members = self.study_sessions(study)
        if session_id not in members:
            index.put_lookup(list_row(_STUDY_ROW, study, [*members, session_id]))
        self.history._record(EVENT_TYPE, session_id, entry["id"], entry)
        with self._lock:
            self._entries.setdefault(session_id, []).append(entry)
        bus = getattr(self.app.state, "bus", None)
        if bus is not None:
            from clio_agent.gact.events import Event  # noqa: PLC0415

            # The writer's stream, its tree root's (where the person reads the study) and
            # every branch's that holds entries, so a tab open on any branch stays live.
            root = index.session_root(session_id)
            for member in dict.fromkeys((session_id, root, *members)):
                bus.publish(Event(type="science.entry", session_id=member, payload=entry))


def science_log(app: Any) -> ScienceLog:
    """The app's science log (created on first use)."""

    log = getattr(app.state, "science_log", None)
    if log is None:
        log = ScienceLog(app)
        app.state.science_log = log
    return log


__all__ = ["EVENT_TYPE", "ScienceLog", "entry_id", "science_log"]
