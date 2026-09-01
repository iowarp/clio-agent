"""Prove the CMF server actually HOLDS a push before it counts as written.

A 200 from ``/api/mlmd_push`` is not evidence. The server swallows per-entity
ingest failures -- ``cmf_merger.handle_execution`` and the events loop each wrap
their write in ``except Exception: logger.error(...)`` -- so it can answer
``{"status": "success"}`` having stored nothing. A live qualification recorded
13 executions, zero artifacts and zero events against a completely green
counter for exactly that reason.

Two reads, both bounded, make the counter structural rather than aspirational:

* **executions** -- one page of the stage's most recent rows, matched on
  ``Execution_uuid``. Not on the execution *name*: a live cmf-server's rows
  carry only ``execution_id`` plus an ``execution_properties`` list of
  ``{name, value}`` pairs, with no ``name`` key at all, and matching on name
  produced a false negative against the real server.
* **events** -- existence of an execution says nothing about its edges, and a
  lost INPUT costs the lineage graph a ``b = transform(a)`` input while
  everything else looks healthy. ``POST /mlmd_pull`` accepts an ``exec_uuid``,
  so one execution's events come back in a bounded response; a batch wider than
  :data:`_CONFIRM_MAX_EVENT_PULLS` uses a single unscoped pull instead, keeping
  the REQUEST count bounded either way.

Anything missing raises ``cmf_server_discarded_entities`` so the loss lands in
``ProviderHealth`` instead of passing as a successful write.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from clio_agent.gact.artifacts.provenance.cmf_lineage_rest import cmf_property
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal

#: Floor for the confirmation read's page size, so a one-execution batch still
#: tolerates a few concurrent writers landing between the push and the read.
_CONFIRM_MIN_ROWS = 20

#: Above this many executions-with-events in one batch, the event confirmation
#: switches from one scoped pull each to a single unscoped pull, so the REQUEST
#: count stays bounded even when a backlog flushes at once.
_CONFIRM_MAX_EVENT_PULLS = 8


def _covers(held: list[int], expected: list[int]) -> bool:
    """Whether ``held`` contains at least as many of each event type as expected.

    A multiset check, not equality: one execution legitimately accumulates more
    edges as later batches attach to it, so extra events are fine and missing
    ones are not.
    """
    return all(held.count(event_type) >= expected.count(event_type) for event_type in expected)


class PushConfirmer:
    """Read back one pushed document and refuse what the server did not keep."""

    def __init__(self, config: Any, http: Any) -> None:
        self._config = config
        #: Callable returning the shared ``httpx.Client`` the publisher owns.
        self._client = http

    def _http(self) -> httpx.Client:
        return self._client()

    def confirm(self, document: dict[str, Any], *, evicted: int) -> None:
        """Verify the server holds this document's executions AND their events."""
        self._confirm_held(document, evicted=evicted)

    def _confirm_held(self, document: dict[str, Any], *, evicted: int) -> None:
        """Verify the server actually HOLDS what it just answered success to.

        A 200 is not evidence. The server's ingest swallows per-entity failures
        (``cmf_merger.handle_execution`` wraps the write in
        ``except Exception: logger.error(...)``), so a push can report success
        having stored nothing -- which is exactly how a live run recorded 13
        executions, zero artifacts and zero events against a green counter.

        One bounded read of the stage's most recent executions, checking the
        names just pushed are present, is what makes ``accepted`` structural
        rather than aspirational.

        Args:
            document: The document that was just pushed.
            evicted: Records dropped from the pending bound, for the refusal.

        Raises:
            CMFRefusal: ``cmf_server_discarded_entities`` -- entities are
                missing; ``cmf_server_unreachable`` -- the read-back failed.
        """
        # Match on Execution_uuid, not on the execution name: the dashboard's
        # execution rows carry only ``execution_id`` plus an
        # ``execution_properties`` list of {name, value} pairs -- there is no
        # ``name`` key to match (verified against a live cmf-server).
        expected = {
            str((execution.get("properties") or {}).get("Execution_uuid") or "")
            for pipeline in document.get("Pipeline") or []
            for stage in pipeline.get("stages") or []
            for execution in stage.get("executions") or []
        }
        expected.discard("")
        if not expected:
            return
        stage_name = f"{self._config.pipeline_name}/artifacts"
        try:
            response = self._http().get(
                f"{self._config.server_url.strip().rstrip('/')}"
                f"/api/executions-by-stage/{quote(self._config.pipeline_name, safe='')}",
                params={
                    "stage_name": stage_name,
                    "active_page": 1,
                    # Bounded: the just-pushed rows are the most recent, so one
                    # page sized to the batch (plus headroom for concurrent
                    # writers) is enough to see them.
                    "record_per_page": max(len(expected) * 2, _CONFIRM_MIN_ROWS),
                    "sort_order": "DESC",
                    "filter_value": "",
                },
                timeout=self._config.publish_timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CMFRefusal(
                "cmf_server_unreachable",
                f"push confirmation read failed: {type(exc).__name__}: {exc}",
                server_url=self._config.server_url,
                evicted_records=evicted,
            ) from exc
        rows = payload.get("items") if isinstance(payload, dict) else payload
        held: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            # A merged execution's uuid property is a comma-joined union.
            uuids = str(cmf_property(row, "Execution_uuid") or "")
            held.update(part for part in uuids.split(",") if part)
        missing = sorted(expected - held)
        if missing:
            raise CMFRefusal(
                "cmf_server_discarded_entities",
                (
                    f"the CMF server answered success but does not hold "
                    f"{len(missing)} of {len(expected)} pushed executions"
                ),
                missing=missing[:10],
                missing_count=len(missing),
                pipeline_name=self._config.pipeline_name,
                evicted_records=evicted,
            )
        self._confirm_events(document, evicted=evicted)

    def _confirm_events(self, document: dict[str, Any], *, evicted: int) -> None:
        """Verify the server holds the EVENTS, not just the executions.

        Existence of an execution says nothing about its edges: an event can be
        dropped on its own (``cmf_merger.handle_event`` has its own swallowing
        ``except``), and a lost INPUT costs the lineage graph a
        ``b = transform(a)`` input while everything else looks healthy.

        ``POST /mlmd_pull`` accepts an ``exec_uuid``, so one execution's events
        come back in a bounded response. A batch wider than
        :data:`_CONFIRM_MAX_EVENT_PULLS` uses a single unscoped pull instead, so
        the REQUEST count stays bounded either way.

        Args:
            document: The document that was just pushed.
            evicted: Records dropped from the pending bound, for the refusal.

        Raises:
            CMFRefusal: An expected event is not held, or the read failed.
        """
        wanted: dict[str, list[int]] = {}
        for pipeline in document.get("Pipeline") or []:
            for stage in pipeline.get("stages") or []:
                for execution in stage.get("executions") or []:
                    events = execution.get("events") or []
                    if not events:
                        continue
                    uuid = str((execution.get("properties") or {}).get("Execution_uuid") or "")
                    if uuid:
                        wanted[uuid] = sorted(int(event.get("type") or 0) for event in events)
        if not wanted:
            return
        held = self._pull_events(sorted(wanted), evicted=evicted)
        short = {
            uuid: {"expected": types, "held": held.get(uuid, [])}
            for uuid, types in wanted.items()
            # Held events are a MULTISET check: the same execution legitimately
            # accumulates edges across batches, so more is fine and fewer is not.
            if not _covers(held.get(uuid, []), types)
        }
        if short:
            raise CMFRefusal(
                "cmf_server_discarded_entities",
                (
                    f"the CMF server answered success but is missing events on "
                    f"{len(short)} of {len(wanted)} pushed executions"
                ),
                executions=sorted(short)[:10],
                detail={key: short[key] for key in sorted(short)[:5]},
                pipeline_name=self._config.pipeline_name,
                evicted_records=evicted,
            )

    def _pull_events(self, uuids: list[str], *, evicted: int) -> dict[str, list[int]]:
        """Read back the event types the server holds per execution uuid."""
        scoped = len(uuids) <= _CONFIRM_MAX_EVENT_PULLS
        requests: list[dict[str, Any]] = (
            [{"pipeline_name": self._config.pipeline_name, "exec_uuid": uuid} for uuid in uuids]
            if scoped
            else [{"pipeline_name": self._config.pipeline_name}]
        )
        held: dict[str, list[int]] = {}
        for body in requests:
            try:
                response = self._http().post(
                    f"{self._config.server_url.strip().rstrip('/')}/mlmd_pull",
                    json=body,
                    timeout=self._config.publish_timeout_s,
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise CMFRefusal(
                    "cmf_server_unreachable",
                    f"event confirmation read failed: {type(exc).__name__}: {exc}",
                    server_url=self._config.server_url,
                    evicted_records=evicted,
                ) from exc
            # The route is declared HTMLResponse, so the body can arrive as a
            # JSON string containing the document rather than as an object.
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            for pipeline in (payload or {}).get("Pipeline") or []:
                for stage in pipeline.get("stages") or []:
                    for execution in stage.get("executions") or []:
                        uuid = str((execution.get("properties") or {}).get("Execution_uuid") or "")
                        for part in uuid.split(","):
                            if part:
                                held.setdefault(part, []).extend(
                                    int(event.get("type") or 0)
                                    for event in execution.get("events") or []
                                )
        return {uuid: sorted(types) for uuid, types in held.items()}


__all__ = ["PushConfirmer"]
