"""Keep the native clio-core client from exiting the CLIO server process.

The clio-core native client ends the whole process on some failures instead of
returning an error: its fatal log level calls ``exit(1)`` (``HLOG(kFatal, ...)`` in
``clio_ctp/util/logging.h``). Two of those are reachable from ``clio_init(kClient)``
before the client ever talks to the daemon, and both were reproduced against
iowarp-core 2.2.1:

- **An embedded runtime.** ``CLIO_WITH_RUNTIME=1`` in the environment makes the client
  start its OWN runtime (``ClioInitImpl``). With the machine's daemon already on the
  port (a host daemon owned by another process), the in-process runtime's port check
  fails and logs ``FATAL ... kill -9 <PID>``: exit(1), 0.6 s after the attach began.
  CLIO always attaches as a pure client to the one shared daemon, so this module
  removes the variable before the attach and records that it did.
- **A config the native parser rejects.** ``clio_init`` loads ``$CLIO_SERVER_CONF``
  with yaml-cpp; a parse error is ``HLOG(kFatal, e.what())``: exit(1). The config the
  client attaches with can be the running daemon's own (first config wins), which CLIO
  checks for existence but does not parse the way the native loader does.

:func:`preflight_native_client` runs the native client's startup in a short-lived
child process first, with ``CLIO_WAIT_SERVER=0`` so the child stops right before it
would contact the daemon (it never registers with it). If the child dies, the server
does not attach: it degrades ARC loudly with ``clio_core_native_client_exit`` and the
child's output, instead of exiting.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Environment variables that make the native client start an in-process runtime.
EMBEDDED_RUNTIME_ENV = ("CLIO_WITH_RUNTIME",)
#: Typed reason recorded when such a variable is removed before the attach.
CLIO_CORE_EMBEDDED_RUNTIME_ENV_REMOVED = "clio_core_embedded_runtime_env_removed"

_DONE_MARKER = "CLIO_NATIVE_PREFLIGHT_RETURNED"
_removed_lock = threading.Lock()
_removed: dict[str, str] = {}
_OUTPUT_TAIL_CHARS = 2000

#: Suffixes of a native extension module (the code that can call ``exit``).
_NATIVE_SUFFIXES = (".pyd", ".so", ".dylib")

# The child: initialise Winsock before the binding loads (#914), then run the native
# client's startup with no wait for the daemon. Reaching the marker means clio_init
# returned (False is expected: it was told not to wait) instead of exiting. The module
# is the one the attach itself calls, imported by name (``{{module}}``).
_CHILD_SOURCE = f"""
import importlib, socket
socket.socket().close()
import iowarp_core  # noqa: F401 - library preload; must precede the extension
cte = importlib.import_module({{module!r}})
init = getattr(cte, "clio_init", None) or cte.chimaera_init
mode = (getattr(cte, "RuntimeMode", None) or cte.ChimaeraMode).kClient
init(mode, False)
print("{_DONE_MARKER}", flush=True)
import os
os._exit(0)
"""


@dataclass(frozen=True)
class NativePreflightResult:
    """What the preflight child did.

    Attributes:
        returned: ``clio_init`` returned in the child (it did not end the process).
        exit_code: The child's exit code (``None`` when it timed out).
        output: The tail of the child's combined stdout and stderr.
    """

    returned: bool
    exit_code: int | None
    output: str
    skipped_reason: str = ""


Runner = Callable[[list[str], Mapping[str, str], float], tuple[int | None, str]]


def _run_child(argv: list[str], env: Mapping[str, str], timeout_s: float) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter + in-module source
            argv,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return None, _text(exc.stdout) + _text(exc.stderr)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _text(value: str | bytes | None) -> str:
    # TimeoutExpired carries whatever was captured, as bytes even with text=True.
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def remove_embedded_runtime_env(environ: "os._Environ[str] | dict[str, str]") -> dict[str, str]:
    """Remove the variables that would make the native client embed a runtime.

    Returns what was removed (name -> value), logged with the typed reason
    ``clio_core_embedded_runtime_env_removed``: never a silent change.
    """
    removed = {name: environ.pop(name) for name in EMBEDDED_RUNTIME_ENV if name in environ}
    if removed:
        with _removed_lock:
            _removed.update(removed)
        logger.warning(
            "clio-core: removed %s before the native attach (reason=%s): CLIO attaches as "
            "a pure client to the machine's shared daemon; an embedded runtime would "
            "collide with it on the port and the native client would exit the process",
            ", ".join(f"{name}={value!r}" for name, value in removed.items()),
            CLIO_CORE_EMBEDDED_RUNTIME_ENV_REMOVED,
        )
    return removed


def removed_embedded_runtime_env() -> dict[str, str]:
    """What :func:`remove_embedded_runtime_env` removed in this process (for the doctor)."""
    with _removed_lock:
        return dict(_removed)


def reset_removed_embedded_runtime_env() -> None:
    """Forget the removal record (test seam; production never resets)."""
    with _removed_lock:
        _removed.clear()


def preflight_window_s(attach_window_s: float) -> float:
    """The bound on the preflight child: the attach window, never below the default stall.

    The child never waits on the daemon (``CLIO_WAIT_SERVER=0``), so its bound only
    guards against a wedged interpreter or library load; a short attach window (a test,
    a tuned deployment) must not turn a slow interpreter start into a failed attach.
    """
    from clio_agent.arc.rpc_liveness import _DEFAULT_STALL_AFTER_S  # noqa: PLC0415

    return max(attach_window_s, _DEFAULT_STALL_AFTER_S)


def native_origin(cte: object) -> str:
    """The file of the native extension ``cte`` is, or ``""`` when it has no native code."""
    spec = getattr(cte, "__spec__", None)
    origin = str(getattr(spec, "origin", "") or getattr(cte, "__file__", "") or "")
    return origin if origin.lower().endswith(_NATIVE_SUFFIXES) else ""


def preflight_native_client(
    cte: object,
    *,
    config_path: str,
    timeout_s: float,
    runner: Runner | None = None,
) -> NativePreflightResult:
    """Run ``cte``'s native client startup in a child process; report whether it returned.

    ``cte`` is the module the attach will call -- the same object, so the preflight
    checks exactly what the attach runs. A module with no native extension behind it
    has no code that can exit the process, so there is nothing to run in a child: the
    result says ``returned`` with ``skipped_reason="no_native_library"``.

    Args:
        cte: The native client module the attach is about to call.
        config_path: The config the real attach will use (``$CLIO_SERVER_CONF``).
        timeout_s: Bound on the child (it does not wait for the daemon, so this only
            covers interpreter start, the library load and the config parse).
        runner: Test seam for the child process.

    Returns:
        The child's outcome; ``returned`` is ``False`` when the native client ended it.
    """
    if not native_origin(cte):
        return NativePreflightResult(
            returned=True, exit_code=None, output="", skipped_reason="no_native_library"
        )
    module = str(getattr(cte, "__name__", "") or "clio_cte_core_ext")
    env = dict(os.environ)
    env["CLIO_SERVER_CONF"] = config_path
    env["CLIO_WAIT_SERVER"] = "0"  # stop before contacting the daemon
    env.setdefault("CTP_LOG_LEVEL", "error")
    source = _CHILD_SOURCE.format(module=module)
    code, output = (runner or _run_child)([sys.executable, "-c", source], env, timeout_s)
    returned = code == 0 and _DONE_MARKER in output
    tail = output.replace(_DONE_MARKER, "").strip()[-_OUTPUT_TAIL_CHARS:]
    return NativePreflightResult(returned=returned, exit_code=code, output=tail)
