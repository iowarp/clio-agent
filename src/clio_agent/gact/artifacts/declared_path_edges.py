"""Provenance for the path arguments a tool's server declared (``ai.iowarp.clio/paths``).

The used-edge detector (:func:`clio_agent.gact.artifacts.transform_edges.detect_used_edges`)
resolves path-looking arguments against the workspace and this machine's filesystem. That
is wrong for a path the server declared to be on another machine (it names nothing here, and
a relative one could even match an unrelated local file), for one declared not to be a file,
and for a declared local write (an output, never an input). Those arguments leave the local
scan; a successful call's declared remote read becomes one ``used`` edge recording where the
input lives, without reading, hashing or minting anything:

    external_ref = "remote:<host>:<path>"  (``remote:<path>`` when no host is declared)
    evidence = schema-arg, note = "remote_input_not_observed"

A declared local read stays in the local scan, as an explicit file-input contract
(:func:`clio_agent.gact.artifacts.external_inputs.external_file_input_args`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge
from clio_agent.tools.tool_path_contract import (
    ACCESS_READ,
    MACHINE_LOCAL,
    MACHINE_REMOTE,
    call_declared_paths,
)

#: The ``note`` of a used edge for an input on another machine.
REMOTE_INPUT_NOTE = "remote_input_not_observed"

_MAX_VALUES = 64


class DeclaredPathScan(NamedTuple):
    """Edges for declared remote reads, and the arguments the local scan must skip."""

    edges: list[ProvEdge]
    skip: frozenset[str]


def _values(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else (value,)
    return [v.strip() for v in items[:_MAX_VALUES] if isinstance(v, str) and v.strip()]


def remote_ref(path: str, host: str = "") -> str:
    """The ``external_ref`` of an input on another machine."""

    return f"remote:{host}:{path}" if host else f"remote:{path}"


def declared_path_scan(
    tool_name: str, args: Mapping[str, Any], *, ok: bool = True
) -> DeclaredPathScan:
    """Scan the running call's declared path arguments.

    Args:
        tool_name: The called tool (its declaration is bound for the call).
        args: The call's arguments.
        ok: Whether the call succeeded; a failed call establishes no consumption, so it
            records no edge (the arguments still leave the local scan).
    """

    edges: list[ProvEdge] = []
    skip: set[str] = set()
    seen: set[str] = set()
    for arg, declared in call_declared_paths(tool_name).items():
        if declared.machine == MACHINE_LOCAL and declared.access == ACCESS_READ:
            continue  # an explicit local file input: the local scan owns it
        skip.add(arg)
        if not ok or declared.machine != MACHINE_REMOTE or declared.access != ACCESS_READ:
            continue
        for raw in _values(args.get(arg)):
            ref = remote_ref(raw, declared.host)
            if ref in seen:
                continue
            seen.add(ref)
            edges.append(
                ProvEdge(
                    role=EdgeRole.USED,
                    evidence=EdgeEvidence.SCHEMA_ARG,
                    external_ref=ref,
                    path=raw,
                    name=PurePosixPath(raw).name,
                    arg=arg,
                    note=REMOTE_INPUT_NOTE,
                )
            )
    return DeclaredPathScan(edges, frozenset(skip))


__all__ = ["REMOTE_INPUT_NOTE", "DeclaredPathScan", "declared_path_scan", "remote_ref"]
