"""Report which Python modules import in this interpreter, as JSON on stdout.

Imported modules may print to stdout (``import fitz`` prints a deprecation warning),
so imports write to stderr and only the JSON reaches stdout.
"""

import importlib
import json
import os
import sys

# Check the interpreter's own modules, not this stack's files beside the script.
here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry or ".") != here]
result = os.fdopen(os.dup(1), "w", encoding="utf-8")
sys.stdout.flush()
os.dup2(2, 1)
sys.stdout = sys.stderr
checks = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except (ImportError, OSError) as error:
        checks[name] = {"status": "missing", "detail": str(error)}
    else:
        checks[name] = {"status": "ready"}
sys.stderr.flush()
result.write(json.dumps(checks))
result.flush()
