"""``GET /v1/system/problem-report`` -- the "Report a problem" page's report.

* ``GET /v1/system/problem-report`` answers the redacted report as JSON: the
  structured sections the page previews, the manifest, and ``text``, the same
  report as one plain-text document (what Copy and Download carry).
* ``GET /v1/system/problem-report/download`` answers that text as a ``.txt``
  attachment, for a client that downloads straight from the service.

Query: ``lines`` (log tail length, default 500, at most 5000), ``session_id`` (a
conversation the person picked), ``include_messages`` (that conversation's
message text, off unless the person ticks it), ``client`` (``desktop`` / ``web``)
and ``client_version``. The report itself is built and redacted by
:mod:`clio_agent.gact.problem_report`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse

from clio_agent.gact.routes.content_disposition import content_disposition
from clio_agent.runtime.log_sources import DEFAULT_TAIL_LINES, MAX_TAIL_LINES


def register_problem_report_routes(app: FastAPI) -> None:
    """Register the report and its download on ``app``."""

    from clio_agent.gact.problem_report import build_problem_report  # noqa: PLC0415

    async def _report(
        lines: int,
        session_id: Optional[str],
        include_messages: bool,
        client: Optional[Literal["desktop", "web"]],
        client_version: Optional[str],
    ) -> dict[str, Any]:
        return await build_problem_report(
            app,
            lines=lines,
            session_id=(session_id or "").strip() or None,
            include_messages=include_messages,
            client=client,
            client_version=(client_version or "").strip()[:64] or None,
        )

    @app.get("/v1/system/problem-report", response_model=None)
    async def problem_report(
        lines: int = Query(DEFAULT_TAIL_LINES, ge=1, le=MAX_TAIL_LINES),
        session_id: Optional[str] = None,
        include_messages: bool = False,
        client: Optional[Literal["desktop", "web"]] = None,
        client_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """The redacted diagnostic report with its plain-text rendering."""

        return await _report(lines, session_id, include_messages, client, client_version)

    @app.get("/v1/system/problem-report/download", response_model=None)
    async def problem_report_download(
        lines: int = Query(DEFAULT_TAIL_LINES, ge=1, le=MAX_TAIL_LINES),
        session_id: Optional[str] = None,
        include_messages: bool = False,
        client: Optional[Literal["desktop", "web"]] = None,
        client_version: Optional[str] = None,
    ) -> PlainTextResponse:
        """The same report as a ``.txt`` attachment."""

        report = await _report(lines, session_id, include_messages, client, client_version)
        return PlainTextResponse(
            report["text"],
            headers={
                "Content-Disposition": content_disposition(report["filename"]),
                "Cache-Control": "no-store",
            },
        )


__all__ = ["register_problem_report_routes"]
