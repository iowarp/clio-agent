"""Stable identities for observer ledger records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _tool_call_event_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable identity for de-duplicating tool telemetry events."""
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        return "__call_id__", call_id
    return _tool_call_name_args_key(call)


def _tool_call_name_args_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a tool-name/arguments identity for posthoc trajectory rows."""

    name = str(call.get("name") or call.get("tool") or "")
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return name, encoded_args
