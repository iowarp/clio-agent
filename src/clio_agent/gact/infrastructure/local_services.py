"""Stop the supervised services this CLIO started on this computer.

A supervised service's worker deliberately leaves the server's process tree
(:func:`node_service.launch` detaches it so it survives an SSH disconnect), so
stopping the server leaves the service running, holding the Agent's interpreter
open. ``clio stop`` runs this afterwards (``clio-agent services stop``, or
``python -m clio_agent.gact.infrastructure.local_services stop``).

Every stop goes through the serialized controller (:func:`node_service.control`)
with the owner recorded in the service directory, so only an exact, identity-checked
owned process is signalled and the receipt ends in the ordinary ``stopped`` phase:
the next boot starts the service as usual. Nothing is uninstalled or deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure import node_service

#: The infrastructure ledger's target id for this computer.
LOCAL_TARGET = "local"
#: Errors one service's stop may raise; the others are still stopped.
STOP_ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError)


@dataclass(frozen=True)
class LocalService:
    """One service directory this host's CLIO installed and owns."""

    root: str
    owner: str

    @property
    def name(self) -> str:
        return Path(self.root).name


@dataclass(frozen=True)
class StopOutcome:
    """What stopping one running service did."""

    service: LocalService
    pid: int
    error: str | None = None


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ledger_path() -> Path:
    """The server's infrastructure ledger (beside its sessions store)."""
    from clio_agent.gact.sessions import _default_store_path  # noqa: PLC0415 - heavy import

    return _default_store_path().parent / "infrastructure.json"


def _ledger_locations(ledger: Path) -> tuple[list[str], list[str]]:
    """Service directories and service-data roots the ledger records for this computer."""
    payload = _read_json(ledger)
    if not isinstance(payload, dict):
        return [], []
    directories: list[str] = []
    roots: list[str] = []
    local = (payload.get("targets") or {}).get(LOCAL_TARGET)
    storage = local.get("storage") if isinstance(local, dict) else None
    if isinstance(storage, dict):
        if storage.get("service_data"):
            roots.append(str(storage["service_data"]))
        if storage.get("root"):
            roots.append(os.path.join(str(storage["root"]), "services"))
    services = payload.get("services")
    for record in services.values() if isinstance(services, dict) else ():
        if not isinstance(record, dict) or record.get("target_id") != LOCAL_TARGET:
            continue
        configured = (record.get("configuration") or {}).get("storage.service_directory")
        if configured:
            directories.append(str(configured))
        directories.extend(
            str(row["ref"])
            for row in record.get("owned_resources") or ()
            if isinstance(row, dict) and row.get("kind") == "directory" and row.get("ref")
        )
    return directories, roots


def default_service_data_roots() -> list[str]:
    """Where this computer's managed services install by default (``<root>/<host>/<name>``)."""
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.gact.infrastructure.service_paths import local_service_root  # noqa: PLC0415

    data = str(paths.user_data_dir())
    return [os.path.join(data, "services"), os.path.join(local_service_root(data), "services")]


def candidate_directories(
    service_data_roots: Iterable[str], directories: Iterable[str] = ()
) -> list[Path]:
    """Every ``<service data>/<host>/<name>`` directory, plus explicitly recorded ones."""
    found: list[Path] = [Path(raw) for raw in directories if raw]
    for raw in service_data_roots:
        base = Path(raw)
        try:
            hosts = [host for host in base.iterdir() if host.is_dir()] if base.is_dir() else []
            for host in hosts:
                found.extend(path for path in host.iterdir() if path.is_dir())
        except OSError:
            continue
    unique: dict[str, Path] = {}
    for path in found:
        unique.setdefault(os.path.normcase(os.path.normpath(str(path))), path)
    return list(unique.values())


def owned_services(candidates: Iterable[Path], host: str | None = None) -> list[LocalService]:
    """The candidates whose ownership marker names this host and that exact directory."""
    host = host or socket.gethostname()
    services = []
    for directory in candidates:
        marker = _read_json(directory / "owner.json")
        if not isinstance(marker, dict) or marker.get("host") != host:
            continue
        root, owner = marker.get("root"), marker.get("owner")
        if isinstance(root, str) and isinstance(owner, str) and _same_path(root, str(directory)):
            services.append(LocalService(root=root, owner=owner))
    return services


def local_services(
    *,
    service_data_roots: Iterable[str] | None = None,
    ledger: Path | None = None,
    host: str | None = None,
) -> list[LocalService]:
    """This computer's supervised services: the default layout and the ledger's records."""
    directories, recorded_roots = _ledger_locations(ledger if ledger else ledger_path())
    roots = [
        *(default_service_data_roots() if service_data_roots is None else service_data_roots),
        *recorded_roots,
    ]
    return owned_services(candidate_directories(roots, directories), host)


def running_pid(service: LocalService) -> int:
    """The service's owned worker PID when its recorded identity is live, else 0."""
    receipt = node_service.read_receipt(Path(service.root))
    return int(receipt.get("pid") or 0) if node_service.alive(receipt) else 0


def stop_local_services(
    services: Iterable[LocalService],
    *,
    names: Collection[str] = (),
    running: Callable[[LocalService], int] = running_pid,
    control: Callable[[dict[str, Any]], dict[str, Any]] = node_service.control,
) -> list[StopOutcome]:
    """Stop each running service through the controller; idle ones are left untouched."""
    outcomes = []
    for service in services:
        if names and service.name not in names:
            continue
        try:
            pid = running(service)
            if not pid:
                continue
            control({"root": service.root, "owner": service.owner, "action": "stop"})
        except STOP_ERRORS as error:
            outcomes.append(StopOutcome(service, 0, str(error) or type(error).__name__))
            continue
        outcomes.append(StopOutcome(service, pid))
    return outcomes


def main(argv: Sequence[str] | None = None) -> int:
    """``services stop [NAME ...]``: stop this computer's running supervised services."""
    parser = argparse.ArgumentParser(
        prog="clio-agent services",
        description="Manage the supervised services this CLIO started on this computer.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    stop = commands.add_parser(
        "stop",
        help="Stop running local services (installations are kept; the next boot restarts "
        "the ones CLIO starts automatically).",
    )
    stop.add_argument("names", nargs="*", help="Only these services (directory names)")
    arguments = parser.parse_args(argv)
    failed = False
    for outcome in stop_local_services(local_services(), names=arguments.names):
        if outcome.error:
            failed = True
            print(f"could not stop {outcome.service.name}: {outcome.error}", file=sys.stderr)
        else:
            print(f"stopped {outcome.service.name} (pid {outcome.pid})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
