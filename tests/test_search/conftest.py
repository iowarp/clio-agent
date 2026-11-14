"""Every search test starts with an empty request queue and limits table."""

from __future__ import annotations

import pytest

from clio_agent.search.scheduler import SCHEDULER
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS


@pytest.fixture(autouse=True)
def _fresh_queue():
    SCHEDULER.reset()
    SEARCH_LIMITS.reset()
    yield
    SCHEDULER.reset()
    SEARCH_LIMITS.reset()
