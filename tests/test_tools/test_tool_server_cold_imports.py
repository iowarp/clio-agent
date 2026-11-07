"""A tool server's first call does not import the model stack or the sandbox doctor.

The shell server measured its first output against the result room, which looked up
the active LM by importing dspy (and the OpenAI SDK): about 2 s in a server process
that has no LM. Confining its first command imported the sandbox doctor, which pulled
in config, the LM factory and the A2UI schemas: about 0.55 s more (2026-11).
"""

from __future__ import annotations

import json
import subprocess
import sys

_PROBE = """
import json, sys
from clio_agent.runtime import sandbox
from clio_agent.tools.result_room import ResultRoom
ResultRoom(tokens=1000, chars=4000, reason="probe").fits("x" * 2000)
print(json.dumps({name: name in sys.modules for name in
      ("dspy", "openai", "clio_agent.runtime.sandbox_doctor")}))
"""


def test_measuring_a_result_and_loading_the_sandbox_stay_light() -> None:
    """SABOTAGE: import dspy unconditionally in ``ambient_lm.active_lm``, or import
    the doctor at the bottom of ``runtime/sandbox.py``, and the matching flag is true."""
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    loaded = json.loads(out.strip().splitlines()[-1])
    assert loaded == {"dspy": False, "openai": False, "clio_agent.runtime.sandbox_doctor": False}


def test_the_doctor_functions_are_still_reachable_through_the_sandbox_module() -> None:
    from clio_agent.runtime import sandbox
    from clio_agent.runtime.sandbox import probe_sandbox

    assert callable(probe_sandbox) and callable(sandbox.emit_boot_state_event)
