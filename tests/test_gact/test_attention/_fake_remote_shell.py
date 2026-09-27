"""Test stand-in for ``ssh <node>``: run the one remote command string locally.

``RemoteFile`` invokes ``<prefix> "<quoted remote command>"`` with the reader
script on stdin, exactly as ``ssh host "<command>"`` would hand it to the
node's shell. This executes that command string with this machine's Python in
place of the node's ``python3``, so the real script and the real quoting run.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

argv = shlex.split(sys.argv[-1])
if argv[0] != "python3":
    sys.exit(f"unexpected remote command {argv[0]!r}")
sys.exit(subprocess.run([sys.executable, *argv[1:]], stdin=sys.stdin, check=False).returncode)
