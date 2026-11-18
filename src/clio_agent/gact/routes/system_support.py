"""Support routes registered with the system routes (owner of their registration).

Registers the routes that help a person keep CLIO working: the latest published
release (``routes/latest_release.py``) and the "Report a problem" report
(``routes/problem_report.py``). ``routes/system.py`` calls this one factory, so a
new support route does not grow that ratcheted module.
"""

from __future__ import annotations

from fastapi import FastAPI

from clio_agent.gact.routes.latest_release import register_latest_release_routes
from clio_agent.gact.routes.problem_report import register_problem_report_routes


def register_system_support_routes(app: FastAPI) -> None:
    """Register the release check and the problem report on ``app``."""

    register_latest_release_routes(app)
    register_problem_report_routes(app)


__all__ = ["register_system_support_routes"]
