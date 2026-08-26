"""Flowcept projection and query adapter.

Flowcept owns transport, buffering, persistence, and querying.  This module only
maps CLIO semantic events to Flowcept records and maps Flowcept query results
back to CLIO's provider-neutral execution model.
"""

from __future__ import annotations

import fnmatch
import importlib
import os
import socket
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import patch

from clio_agent.gact.provenance.normalization import normalize_semantic_events
from clio_agent.gact.provenance.protocol import ProviderReceipt
from clio_agent.gact.semantic_events import SemanticEvent

_ID_NAMESPACE = uuid.UUID("21e8ace1-bf4f-4759-aef7-eb8aa53b953a")
_CONTENT_KEYS = {
    "answer",
    "arguments",
    "args",
    "content",
    "delta",
    "input",
    "observation",
    "output",
    "prompt",
    "question",
    "raw",
    "reasoning",
    "reasoning_content",
    "response",
    "result",
    "stderr",
    "stdout",
    "text",
    "thought",
    "tool_args",
    "trajectory",
    "transcript",
}
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "bearer_token",
    "credential",
    "credential_ref",
    "password",
    "refresh_token",
    "secret",
}
_CORRELATION_KEYS = (
    "tool_call_id",
    "expert_span_id",
    "step_span_id",
    "invocation_id",
    "attempt_id",
    "task_id",
)


@dataclass(frozen=True)
class FlowceptProviderConfig:
    """CLIO-owned projection policy; connectivity remains Flowcept-owned."""

    settings_path: str = ""
    workflow_scope: str = "session"
    campaign_scope: str = "session"
    campaign_id: str = ""
    privacy: str = "metadata"
    include_events: tuple[str, ...] = ("*",)
    exclude_events: tuple[str, ...] = ("lm.token.delta", "thinking.*")
    check_safe_stops: bool = True

    def __post_init__(self) -> None:
        if self.workflow_scope not in {"session", "process"}:
            raise ValueError("Flowcept workflow_scope must be 'session' or 'process'")
        if self.campaign_scope not in {"session", "workspace", "agent"}:
            raise ValueError("Flowcept campaign_scope must be session, workspace, or agent")
        if self.privacy not in {"metadata", "redacted", "full"}:
            raise ValueError("Flowcept privacy must be metadata, redacted, or full")


def _stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, f"clio:{kind}:{value}"))


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _record_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return _epoch(str(value or ""))


def _windows_uname() -> tuple[str, str, str, str, str]:
    """Return the five-field POSIX uname shape expected by Flowcept/Numpy."""
    if sys.platform != "win32":  # pragma: no cover - Windows-only shim
        raise RuntimeError("_windows_uname is Windows-only")
    version = sys.getwindowsversion()
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "unknown")
    return (
        "Windows",
        socket.gethostname(),
        f"{version.major}.{version.minor}",
        str(version),
        machine,
    )


def _safe_value(value: Any, *, redact: bool) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if str(key).lower().replace("-", "_") in _SECRET_KEYS
                or (redact and str(key).lower() in _CONTENT_KEYS)
                else _safe_value(item, redact=redact)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_value(item, redact=redact) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _correlation_id(event: SemanticEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in _CORRELATION_KEYS:
        if payload.get(key):
            return str(payload[key])
    return event.turn_id or event.span_id


def _public_document(document: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _safe_value(value, redact=False)
        for key, value in document.items()
        if str(key) not in {"_id", "flowcept_settings"}
    }


class FlowceptProvenanceProvider:
    """Optional downstream Flowcept provider with explicit CLIO correlation."""

    name = "flowcept"
    durable = False
    queryable = True
    # Flowcept buffers records asynchronously (AutoflushBuffer +
    # interceptor/MQ pipeline: flowcept/commons/autoflush_buffer.py) and
    # exposes NO repeatable drain primitive -- only
    # Flowcept.stop()/BaseInterceptor.stop(), a one-time TERMINAL teardown
    # (closes the MQ/DocDB DAOs, sets Flowcept.buffer = None) that would
    # permanently kill this provider if flush() called it. flush() is
    # therefore an honest no-op below, not a fake barrier: a caller relying
    # on flush() as a synchronous-persistence barrier for Flowcept-only
    # delivery can still observe a read-after-write race (e.g.
    # routes/provenance.py's execution-provenance read, right after
    # flush(), against query_execution() below).
    flush_durable = False
    flush_note = (
        "Flowcept has no repeatable flush/drain API (only the one-time "
        "terminal Flowcept.stop()); flush() is a no-op and does not "
        "guarantee pending records have reached Flowcept's own storage"
    )

    def __init__(self, config: FlowceptProviderConfig) -> None:
        self.config = config
        if config.settings_path:
            os.environ["FLOWCEPT_SETTINGS_PATH"] = config.settings_path
        try:
            # Flowcept 1.0.3 reads ``os.uname()[0]`` while importing configs.
            # Windows has no os.uname, although the remainder of Flowcept works
            # on Python 3.12. Scope the compatibility value to these imports and
            # restore ``os`` immediately afterward.
            import_context = (
                patch.object(os, "uname", _windows_uname, create=True)
                if not hasattr(os, "uname")
                else nullcontext()
            )
            with import_context:
                module = importlib.import_module("flowcept")
                controller_module = importlib.import_module(
                    "flowcept.flowcept_api.flowcept_controller"
                )
                self._task_module = importlib.import_module("flowcept.instrumentation.task_capture")
                self._workflow_module = importlib.import_module(
                    "flowcept.commons.flowcept_dataclasses.workflow_object"
                )
                self._vocabulary = importlib.import_module("flowcept.commons.vocabulary")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Flowcept provider selected but Flowcept is not installed; "
                "install clio-agent[flowcept]"
            ) from exc

        self._flowcept_class = controller_module.Flowcept
        self._flowcept_module = module
        runtime_workflow_id = _stable_id("runtime", f"{os.getpid()}")
        self._runtime = self._flowcept_class(
            workflow_id=runtime_workflow_id,
            workflow_name="CLIO provenance transport",
            start_persistence=True,
            save_workflow=False,
            check_safe_stops=config.check_safe_stops,
        )
        self._runtime.start()
        self._sessions: dict[str, dict[str, str]] = {}
        self._published_workflows: set[str] = set()
        self._workflow_status: dict[str, str] = {}
        self._published_agents: set[str] = set()
        self._closed = False

    def _included(self, event_type: str) -> bool:
        included = any(
            fnmatch.fnmatchcase(event_type, pattern) for pattern in self.config.include_events
        )
        excluded = any(
            fnmatch.fnmatchcase(event_type, pattern) for pattern in self.config.exclude_events
        )
        return included and not excluded

    def _remember_session(self, event: SemanticEvent) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        row = self._sessions.setdefault(event.session_id, {})
        row.setdefault("workspace_id", event.workspace_id)
        row.setdefault("started_at", event.occurred_at)
        if event.event_type == "session.created":
            row["parent_session_id"] = str(payload.get("parent_session_id") or "")
            raw_agent = payload.get("agent")
            agent: dict[str, Any] = raw_agent if isinstance(raw_agent, dict) else {}
            row["agent_id"] = str(agent.get("id") or payload.get("agent_id") or "")

    def _root_session_id(self, session_id: str) -> str:
        seen: set[str] = set()
        current = session_id
        while current and current not in seen:
            seen.add(current)
            parent = self._sessions.get(current, {}).get("parent_session_id", "")
            if not parent:
                return current
            current = parent
        return session_id

    def _campaign_id(self, event: SemanticEvent) -> str:
        if self.config.campaign_id:
            return self.config.campaign_id
        if self.config.campaign_scope == "workspace":
            value = event.workspace_id or "default"
        elif self.config.campaign_scope == "agent":
            value = str(event.actor.get("agent_id") or "")
            value = value or self._sessions.get(event.session_id, {}).get("agent_id", "main")
        else:
            value = self._root_session_id(event.session_id)
        return _stable_id(f"campaign-{self.config.campaign_scope}", value)

    def _workflow_id(self, session_id: str) -> str:
        if self.config.workflow_scope == "process":
            return _stable_id("workflow-process", str(os.getpid()))
        return _stable_id("workflow-session", session_id)

    def _publish_workflow(self, event: SemanticEvent) -> tuple[str, str]:
        workflow_id = self._workflow_id(event.session_id)
        campaign_id = self._campaign_id(event)
        if workflow_id in self._published_workflows:
            return workflow_id, campaign_id
        workflow = self._workflow_module.WorkflowObject(
            workflow_id=workflow_id,
            name=(
                "CLIO process"
                if self.config.workflow_scope == "process"
                else f"CLIO session {event.session_id}"
            ),
        )
        workflow.campaign_id = campaign_id
        workflow.started_at = _epoch(event.occurred_at)
        workflow.status = self._vocabulary.Status.RUNNING
        workflow.subtype = "agentic_workflow"
        parent_session_id = self._sessions.get(event.session_id, {}).get("parent_session_id", "")
        if parent_session_id and self.config.workflow_scope == "session":
            workflow.parent_workflow_id = self._workflow_id(parent_session_id)
        workflow.custom_metadata = {
            "clio": {
                "session_id": event.session_id,
                "workspace_id": event.workspace_id,
                "workflow_scope": self.config.workflow_scope,
                "campaign_scope": self.config.campaign_scope,
                "schema_version": event.schema_version,
            }
        }
        self._runtime._first_interceptor.send_workflow_message(workflow)
        self._published_workflows.add(workflow_id)
        self._workflow_status[workflow_id] = "RUNNING"
        return workflow_id, campaign_id

    def _publish_agent(
        self, event: SemanticEvent, workflow_id: str, campaign_id: str
    ) -> tuple[str, str]:
        source = str(event.payload.get("source_agent_id") or "")
        original = str(event.actor.get("agent_id") or event.payload.get("expert_id") or "")
        if not original:
            return "", source
        agent_id = _stable_id("agent", f"{event.session_id}:{original}")
        if agent_id not in self._published_agents:
            self._runtime.save_agent(
                name=original,
                agent_id=agent_id,
                workflow_id=workflow_id,
                campaign_id=campaign_id,
            )
            self._published_agents.add(agent_id)
        source_id = _stable_id("agent", f"{event.session_id}:{source}") if source else ""
        return agent_id, source_id

    def _metadata(self, event: SemanticEvent, workflow_id: str, campaign_id: str) -> dict[str, Any]:
        clio = {
            "event_id": event.span_id,
            "event_type": event.event_type,
            "event_status": event.status,
            "schema_version": event.schema_version,
            "session_id": event.session_id,
            "workspace_id": event.workspace_id,
            "trace_id": event.trace_id,
            "turn_id": event.turn_id,
            "span_id": event.span_id,
            "parent_span_id": event.parent_span_id,
            "correlation_id": _correlation_id(event),
            "workflow_id": workflow_id,
            "campaign_id": campaign_id,
            "summary": event.summary,
            "actor": _safe_value(event.actor, redact=True),
            "subject": _safe_value(event.subject, redact=True),
            "provider": _safe_value(event.provider, redact=True),
            "privacy": self.config.privacy,
        }
        if self.config.privacy != "metadata":
            clio["payload"] = _safe_value(
                event.payload,
                redact=self.config.privacy == "redacted",
            )
        return {"clio": clio}

    def emit(self, event: SemanticEvent) -> ProviderReceipt:
        """Project one semantic event through Flowcept's normal runtime."""
        if not self._included(event.event_type):
            return ProviderReceipt.FILTERED
        self._remember_session(event)
        workflow_id, campaign_id = self._publish_workflow(event)
        if (
            self.config.workflow_scope == "session"
            and event.event_type == "turn.started"
            and self._workflow_status.get(workflow_id) != "RUNNING"
        ):
            self._update_workflow(event, workflow_id, campaign_id, terminal=False)
        agent_id, source_agent_id = self._publish_agent(event, workflow_id, campaign_id)
        if event.event_type == "session.deleted":
            if self.config.workflow_scope == "session":
                self._update_workflow(event, workflow_id, campaign_id, terminal=True)
            return ProviderReceipt.ACCEPTED

        status = (
            self._vocabulary.Status.ERROR
            if event.status.lower() in {"failed", "error", "cancelled"}
            else self._vocabulary.Status.FINISHED
        )
        subtype = "clio_event"
        if event.event_type.startswith("lm."):
            subtype = self._vocabulary.PROV_AGENT.AI_MODEL_INVOCATION
        elif event.event_type.startswith(("tool.", "react.step")):
            subtype = self._vocabulary.PROV_AGENT.AGENT_TOOL
        elif event.event_type.startswith("artifact."):
            subtype = "artifact_provenance_summary"
        task = self._task_module.FlowceptTask(
            task_id=_stable_id("task", event.span_id),
            workflow_id=workflow_id,
            campaign_id=campaign_id,
            activity_id=event.event_type,
            agent_id=agent_id or None,
            source_agent_id=source_agent_id or None,
            parent_task_id=(
                _stable_id("task", event.parent_span_id) if event.parent_span_id else None
            ),
            subtype=subtype,
            custom_metadata=self._metadata(event, workflow_id, campaign_id),
            started_at=_epoch(event.occurred_at),
            ended_at=_epoch(event.occurred_at),
            status=status,
            capture_telemetry=False,
        )
        del task
        if self.config.workflow_scope == "session" and event.event_type in {
            "turn.completed",
            "turn.failed",
        }:
            self._update_workflow(event, workflow_id, campaign_id, terminal=True)
        return ProviderReceipt.ACCEPTED

    def _update_workflow(
        self,
        event: SemanticEvent,
        workflow_id: str,
        campaign_id: str,
        *,
        terminal: bool,
    ) -> None:
        """Publish session workflow state as turns start and settle."""
        workflow = self._workflow_module.WorkflowObject(
            workflow_id=workflow_id,
            name=f"CLIO session {event.session_id}",
        )
        workflow.campaign_id = campaign_id
        started_at = self._sessions.get(event.session_id, {}).get("started_at", "")
        workflow.started_at = _epoch(started_at)
        failed = event.event_type == "turn.failed" or event.status.lower() in {
            "failed",
            "error",
            "cancelled",
        }
        if terminal:
            workflow.ended_at = _epoch(event.occurred_at)
            workflow.status = (
                self._vocabulary.Status.ERROR if failed else self._vocabulary.Status.FINISHED
            )
            state = "ERROR" if failed else "FINISHED"
        else:
            workflow.status = self._vocabulary.Status.RUNNING
            state = "RUNNING"
        workflow.subtype = "agentic_workflow"
        workflow.custom_metadata = {
            "clio": {
                "session_id": event.session_id,
                "workspace_id": event.workspace_id,
                "state_event_id": event.span_id,
                "state_event_type": event.event_type,
            }
        }
        document = workflow.to_dict()
        if not terminal:
            # Flowcept workflow updates use MongoDB ``$set`` and WorkflowObject.to_dict()
            # omits ``None``. Send the null explicitly so a new turn clears the
            # previous terminal timestamp instead of exposing RUNNING + stale ended_at.
            document["ended_at"] = None
        self._runtime._first_interceptor.intercept(document)
        self._workflow_status[workflow_id] = state

    def services_status(self) -> dict[str, str]:
        """Delegate liveness checks to Flowcept."""
        return dict(self._flowcept_class.services_status())

    def flush(self) -> None:
        """No-op: see the class-level ``flush_durable``/``flush_note``
        comment above for why Flowcept has no safe, repeatable drain to
        proxy through to (only a one-time, terminal ``stop()``)."""
        return

    def close(self) -> None:
        """Drain Flowcept transport and persistence services."""
        if self._closed:
            return
        self._closed = True
        self._runtime.stop()

    def query_execution(
        self,
        *,
        session_id: str,
        child_session_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        """Query Flowcept and normalize its workflow/task/agent records."""
        session_ids = [session_id, *child_session_ids]
        workflow_ids = [self._workflow_id(value) for value in session_ids]
        workflow_ids = list(dict.fromkeys(workflow_ids))
        db = self._flowcept_class.db
        workflows: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        agents: list[dict[str, Any]] = []
        for workflow_id in workflow_ids:
            workflows.extend(db.workflow_query(filter={"workflow_id": workflow_id}) or [])
            tasks.extend(
                db.task_query(
                    filter={"workflow_id": workflow_id},
                    limit=limit + 1,
                    sort=[("started_at", -1)],
                )
                or []
            )
            agents.extend(db.agent_query(filter={"workflow_id": workflow_id}) or [])
        tasks.sort(key=lambda row: _record_timestamp(row.get("started_at")))
        return _normalize_flowcept_records(
            session_id=session_id,
            workflows=workflows,
            tasks=tasks[-limit:],
            agents=agents,
            provider_health=self.services_status(),
            truncated=len(tasks) > limit,
        )


def _normalize_flowcept_records(
    *,
    session_id: str,
    workflows: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    provider_health: dict[str, Any],
    truncated: bool,
) -> dict[str, Any]:
    public_workflows = [_public_document(row) for row in workflows]
    public_agents = [_public_document(row) for row in agents]
    events: list[dict[str, Any]] = []
    records_by_event_id: dict[str, list[dict[str, Any]]] = {}
    for row in tasks:
        public = _public_document(row)
        metadata = public.get("custom_metadata") or {}
        clio = metadata.get("clio") if isinstance(metadata, dict) else {}
        clio = clio if isinstance(clio, dict) else {}
        event_id = str(clio.get("event_id") or public.get("task_id") or "")
        event_type = str(clio.get("event_type") or public.get("activity_id") or "flowcept.task")
        raw_payload = clio.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        events.append(
            {
                "schema_version": str(clio.get("schema_version") or "clio.semantic_event.v1"),
                "event_id": event_id,
                "span_id": str(clio.get("span_id") or event_id),
                "parent_span_id": str(clio.get("parent_span_id") or ""),
                "event_type": event_type,
                "session_id": str(clio.get("session_id") or session_id),
                "workspace_id": str(clio.get("workspace_id") or ""),
                "trace_id": str(clio.get("trace_id") or ""),
                "turn_id": str(clio.get("turn_id") or ""),
                "status": str(
                    clio.get("event_status") or public.get("status") or "unknown"
                ).lower(),
                "summary": str(clio.get("summary") or public.get("activity_id") or "Flowcept task"),
                "actor": clio.get("actor") or {},
                "subject": clio.get("subject") or {},
                "provider": clio.get("provider") or {},
                "payload": {
                    **payload,
                    "workflow_id": str(public.get("workflow_id") or ""),
                    "campaign_id": str(public.get("campaign_id") or ""),
                    "source_agent_id": str(public.get("source_agent_id") or ""),
                    "hostname": str(public.get("hostname") or ""),
                },
                "correlation_id": str(clio.get("correlation_id") or ""),
                "occurred_at": public.get("started_at"),
            }
        )
        records_by_event_id.setdefault(event_id, []).append(public)
    result = normalize_semantic_events(
        events,
        provider="flowcept",
        session_id=session_id,
        provider_health=provider_health,
        limit=max(1, len(events)),
    )
    result["truncated"] = truncated
    result["campaigns"] = _unique_documents(public_workflows, "campaign_id")
    result["workflows"] = _unique_documents(public_workflows, "workflow_id")
    result["agents"] = _unique_documents(public_agents, "agent_id")
    for span in result["spans"]:
        records = [
            record
            for event_id in span["source_event_ids"]
            for record in records_by_event_id.get(str(event_id), [])
        ]
        span["attributes"]["flowcept_records"] = records
    return result


def _unique_documents(documents: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for document in documents:
        value = str(document.get(key) or "")
        if value:
            unique[value] = document
    return list(unique.values())
