"""Inner test for ``test_cte_stuck_daemon.py``: a store op against a SUSPENDED daemon.

Not collected by a normal run (the name does not match ``test_*.py``); the outer test
runs it in a fresh pytest subprocess, which gets its own private clio-core daemon from
the suite conftest. This test suspends that daemon and then calls a store op. The
native binding holds the GIL while it waits for the daemon, so nothing in Python can
interrupt the call: the only way this process ends is the per-call hard bound
(``tests/_cte_bounded.py``) dumping every stack and exiting. The outer test asserts
that happens within seconds.

Contract with the outer test (environment):

* ``CLIO_STUCK_PROBE_OUT`` -- where to write ``{"daemon_pid", "suspended_at"}`` just
  before the blocking call, so the outer test can time the failure and reap the daemon.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil
import pytest

from clio_agent.arc.storage import ClioCoreStore, make_arc_store
from tests._process_hygiene import daemon_pid


def test_suspended_daemon() -> None:
    """Suspend the private daemon, then block in a store op (the bound must end it)."""
    out = Path(os.environ["CLIO_STUCK_PROBE_OUT"])
    store = make_arc_store(backend="cte")
    assert isinstance(store, ClioCoreStore), "the private daemon did not come up"
    store.put("segments", "stuck-probe", b"payload")
    assert store.get("segments", "stuck-probe") == b"payload"  # healthy before

    pid = daemon_pid()
    assert pid is not None, "no private daemon pidfile"
    psutil.Process(pid).suspend()
    out.write_text(json.dumps({"daemon_pid": pid, "suspended_at": time.time()}), encoding="utf-8")

    store.get("segments", "stuck-probe")  # blocks holding the GIL until the bound fires
    pytest.fail("a store op returned from a suspended clio-core daemon")
