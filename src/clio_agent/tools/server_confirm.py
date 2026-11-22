"""The server-confirm handshake's wire keys: plain MCP ``_meta``, readable by any host.

A tool whose server confirms its own side effects declares
``Tool._meta["io.iowarp/confirm"] = {"version": 1}``. When the host skips its own ask for
such a call it sends ``params._meta["io.iowarp/confirm"] = "server"``; the server then
asks exactly once before any side effect (or refuses ``needs_approval`` when it cannot
ask). A call that needed no ask settles with result ``_meta["io.iowarp/confirm"] =
"not_needed"``. Only the declaration's own version is trusted, so an older server is
never deferred to. See ``docs/PERMISSIONS.md`` (Server-confirmed calls).
"""

from collections.abc import Mapping
from typing import Any

CONFIRM_KEY = "io.iowarp/confirm"
CONFIRM_VERSION = 1
REQUEST_FLAG = "server"
NOT_NEEDED = "not_needed"


def _meta_of(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        meta = value.get("_meta", value.get("meta"))
    else:
        meta = getattr(value, "meta", None)
    return meta if isinstance(meta, Mapping) else {}


def declares(tool: Any) -> bool:
    """Whether a listed tool declares that its server confirms the call itself."""
    declared = _meta_of(tool).get(CONFIRM_KEY)
    return isinstance(declared, Mapping) and declared.get("version") == CONFIRM_VERSION


def not_needed(result: Any) -> bool:
    """Whether a flagged call's result says it needed no confirm (it only observed)."""
    return _meta_of(result).get(CONFIRM_KEY) == NOT_NEEDED


__all__ = ["CONFIRM_KEY", "NOT_NEEDED", "REQUEST_FLAG", "declares", "not_needed"]
