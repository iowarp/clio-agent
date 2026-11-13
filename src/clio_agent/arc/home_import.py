"""Bring a CLIO home's records from another home's clio-core store to its own, once.

Before per-home daemons (:mod:`clio_agent.arc.home_runtime`), every home on a machine
attached to the daemon the host bookkeeping named, so a home's sessions, transcripts
and memory could sit in the store of whichever home spawned that daemon. Deleting that
home lost them. A home that now runs on its own daemon (or moves off a daemon whose
spawner is gone) first copies its records over:

1. :func:`export_if_pending`, before connect-or-spawn, checks whether the store the
   host record names is another home's and whether this home has anything that can be
   in it. A child process then reads this home's records out: attached to the running
   daemon without ever spawning, stopping or replacing it, or, when that daemon is
   stopped, from a private copy of its store on a short-lived daemon of its own. The
   source store is never written either way. The records go to
   ``core-import/payload.jsonl``.
2. :func:`import_pending`, right after attach and before anything reads the store,
   writes each record this home's store does not have yet (copy-if-absent, search
   companions included) and merges the sessions into its session index. Progress is
   kept in ``status.json``, so an interrupted import resumes where it stopped;
   ``settled.json`` ends it for good. The boot log and the ``clio_core_attach`` health
   row report it.

Which records are this home's: every session row under its namespace
(:mod:`clio_agent.arc.home_namespace`), the bare rows of its own workspaces or
transcripts that no other home claimed (the rule of the #2176 adoption), their child
sessions, and what those sessions and workspaces keyed. Records nothing identifies
(variants, user memory, the shared default workspace) are copied too: a duplicate is
better than a record hidden from its home. The derived indexes are rebuilt by the store
migrations instead of being copied.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Set in the exporter child: it never imports, sizes or exports itself.
EXPORTER_ENV = "CLIO_HOME_IMPORT_EXPORTER"

#: Typed reasons in the boot log and in health.
HOME_IMPORT_PROGRESS = "clio_core_home_import_progress"
HOME_IMPORT_DONE = "clio_core_home_import_done"
HOME_IMPORT_FAILED = "clio_core_home_import_failed"
HOME_IMPORT_SOURCE_MISSING = "clio_core_home_import_source_missing"
HOME_IMPORT_NOTHING = "clio_core_home_import_nothing"

_COMPANION = ".text"
_PROGRESS_EVERY = 500
_EXPORT_TIMEOUT_S = 1800.0
_SESSION_KIND = "session_records"
_BUCKETS = 64
#: Indexes the store migrations rebuild from the records (their markers are reset).
_DERIVED = frozenset({"task_index", "task_lookup", "task_open", "artifact_index", "recall_index"})
_MEMORY_KINDS = frozenset(
    {"memories", "memory_versions", "memory_proposals", "memory_usage", "memory_creation_runs"}
)
_SHARED_WORKSPACES = frozenset({"ws_default", "ws_global"})
_DROPPED_ENV = (
    "CLIO_USER_DIR",
    "CLIO_ARC_NAMESPACE",
    "CLIO_ARC_STORE_CONFIG",
    "CLIO_CORE_PORT",
    "CLIO_CORE_USER",
    "CLIO_CTE_POOL",
    "CLIO_RUNTIME_STATE_DIR",
    "CLIO_SERVER_CONF",
    "CLIO_SESSIONS_PATH",
    "CLIO_CORE_ATTACH_ONLY",
    "CLIO_DESKTOP_MANAGED",
)

_lock = threading.Lock()
_imported = False


# ---- bookkeeping ---------------------------------------------------------------------


def import_dir() -> Path:
    """This home's import bookkeeping."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

    return paths.user_data_dir() / "core-import"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def status() -> dict[str, Any]:
    """The import's state for health (``{}`` when this home never looked)."""
    root = import_dir()
    return _read_json(root / "settled.json") or _read_json(root / "status.json")


def health_details() -> dict[str, object]:
    """Which daemon this home selected and how its one-time record import went."""
    from clio_agent.arc import home_runtime  # noqa: PLC0415 - cycle

    daemon = home_runtime.snapshot()
    try:
        return {"home_daemon": daemon, "home_import": status()}
    except Exception as exc:  # noqa: BLE001 - forwards: into health; it never fails on it
        return {"home_daemon": daemon, "home_import_error": f"{type(exc).__name__}: {exc}"}


def health_note() -> str:
    """One sentence on the daemon choice and import, for the health row's summary."""
    details = health_details()
    daemon = details.get("home_daemon") or {}
    info: dict[str, Any] = details.get("home_import") or {}  # type: ignore[assignment]
    parts = [f"daemon: {daemon['mode']}"] if isinstance(daemon, dict) and daemon else []
    state = info.get("state")
    if state in ("exporting", "exported", "importing"):
        total = info.get("total_lines")
        done = info.get("done_lines", 0)
        count = f" {done}/{total} records" if total else ""
        parts.append(f"record import {state}{count} from {info.get('source', 'the old store')}")
    elif state == "done":
        parts.append(
            f"record import done: {info.get('created', 0)} copied, "
            f"{info.get('existed', 0)} already present, from {info.get('source')}"
        )
    elif state in ("failed", "source_missing"):
        why = info.get("error") or info.get("detail") or ""
        parts.append(f"record import {state} ({info.get('reason')}): {why}")
    return "; ".join(parts)


def _set_status(**fields: Any) -> dict[str, Any]:
    path = import_dir() / "status.json"
    current = _read_json(path)
    current.update(fields, updated_at=time.time())
    _write_json(path, current)
    return current


def _settle(state: str, reason: str, **fields: Any) -> None:
    data = {**_read_json(import_dir() / "status.json"), **fields}
    data.update(state=state, reason=reason, settled_at=time.time())
    _write_json(import_dir() / "settled.json", data)


def _tag(namespace: str, kind: str) -> str:
    return f"{namespace}/{kind}" if namespace else kind


def _arc_namespace() -> str:
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle

    return conf.resolve(
        "arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str
    ).strip()


def _home_ids() -> tuple[set[str], set[str]]:
    """``(workspace ids, transcript session ids)`` this home holds on file."""
    from clio_agent.gact.sessions import _default_store_path  # noqa: PLC0415 - cycle
    from clio_agent.gact.workspaces import (  # noqa: PLC0415 - cycle
        _default_store_path as _workspaces_path,
    )

    root = _default_store_path().parent
    # The app keeps workspaces beside an explicit sessions path, else in the config dir.
    workspaces: set[str] = set()
    for path in {root / "workspaces.json", _workspaces_path()}:
        rows = _read_json(path).get("workspaces") or ()
        workspaces |= {str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")}
    workspaces -= _SHARED_WORKSPACES
    messages = root / "messages"
    sessions = {path.stem for path in messages.glob("*.json")} if messages.is_dir() else set()
    return workspaces, sessions


# ---- the export (parent side) ----------------------------------------------------------


def export_if_pending() -> None:
    """Read this home's records out of another home's store, once, before attaching.

    Never raises: a failure is reported (typed, in the log and in health) and tried
    again at the next start; this home still starts.
    """
    if os.environ.get(EXPORTER_ENV):
        return
    root = import_dir()
    if (root / "settled.json").exists() or (root / "export.done").exists():
        return
    try:
        _export_pending(root)
    except Exception as exc:  # noqa: BLE001 - forwards: typed status; the home starts
        _set_status(state="failed", reason=HOME_IMPORT_FAILED, error=str(exc))
        logger.error(
            "home import export failed (reason=%s): %s; retried at the next start",
            HOME_IMPORT_FAILED,
            exc,
        )


def _export_pending(root: Path) -> None:
    from clio_agent.arc import home_runtime  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_daemon_version import read_daemon_version  # noqa: PLC0415
    from clio_agent.arc.clio_core_effective_runtime import read_daemon_pid  # noqa: PLC0415
    from clio_agent.arc.home_namespace import home_identity, namespace_for  # noqa: PLC0415

    host = home_runtime.host_runtime_dir(create=False)
    record = read_daemon_version(host)
    if record is None or not record.config_path or home_runtime.record_is_mine(record):
        _settle("nothing", HOME_IMPORT_NOTHING, detail="no other home's store is recorded")
        return
    workspaces, transcripts = _home_ids()
    if not workspaces and not transcripts:
        _settle("nothing", HOME_IMPORT_NOTHING, detail="this home has no sessions on file")
        return
    source = Path(record.config_path)
    pid, live = read_daemon_pid(host)
    request: dict[str, Any] = {
        "source_config": str(source),
        "source_home": record.home,
        "namespace": namespace_for(home_identity()),
        "arc_namespace": _arc_namespace(),
        "workspaces": sorted(workspaces),
        "transcripts": sorted(transcripts),
        "payload": str(root / "payload.jsonl"),
        "done": str(root / "export.done"),
    }
    env = _exporter_env(root)
    if live and pid is not None:
        request["mode"] = "live"
        config = source if source.is_file() else _reconstruct_config(source, pid, root)
        request["config"] = str(config)
        env.update(CLIO_RUNTIME_STATE_DIR=str(host), CLIO_CORE_ATTACH_ONLY="1")
        user = _daemon_user(pid)
        if user is None:
            env.pop("USER", None)
        else:
            env["USER"] = user
    else:
        if not source.is_file() and not _has_store_data(source.parent):
            _settle(
                "source_missing",
                HOME_IMPORT_SOURCE_MISSING,
                source=str(source),
                detail="the store the host record names is gone and no daemon runs it",
            )
            logger.warning(
                "home import: the recorded store %s is gone (reason=%s); nothing to import",
                source,
                HOME_IMPORT_SOURCE_MISSING,
            )
            return
        copy = _private_copy(source.parent, root, host)
        if copy is None:  # the source daemon came back while copying: read it live
            return _export_pending(root)
        request["mode"] = "copy"
        request["config"] = str(copy / "cte.yaml")
        state = root / "runtime"
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        base = os.environ.get("USERNAME") or os.environ.get("USER") or "clio"
        env.update(CLIO_RUNTIME_STATE_DIR=str(state), CLIO_CORE_USER=f"{base}-clio-import-{digest}")
    request_path = root / "request.json"
    _write_json(request_path, request)
    _set_status(
        state="exporting",
        reason=HOME_IMPORT_PROGRESS,
        mode=request["mode"],
        source=str(source),
        source_home=record.home,
    )
    logger.warning(
        "home import: reading this home's records from %s (%s, reason=%s); the source is "
        "not changed",
        source,
        "running daemon" if request["mode"] == "live" else "private copy",
        HOME_IMPORT_PROGRESS,
    )
    try:
        _run_exporter(root, request_path, env)
    finally:
        if request["mode"] == "copy":
            _retire_private_copy(root)
    done = _read_json(root / "export.done")
    _set_status(state="exported", total_lines=done.get("lines", 0), exported=done)
    logger.info(
        "home import: exported %d records of %d sessions (reason=%s)",
        done.get("lines", 0),
        done.get("sessions", 0),
        HOME_IMPORT_PROGRESS,
    )


def _exporter_env(root: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _DROPPED_ENV and not (key.startswith("CLIO_AGENT_") and key.endswith("_DIR"))
    }
    env[EXPORTER_ENV] = "1"
    env["CLIO_AGENT_HOME"] = str(root / "exporter-home")
    return env


def _run_exporter(root: Path, request_path: Path, env: dict[str, str]) -> None:
    (root / "export.done").unlink(missing_ok=True)
    log = root / "export.log"
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.run(  # noqa: S603 - our own module, our own interpreter
            [sys.executable, "-m", "clio_agent.arc.home_import", "export", str(request_path)],
            cwd=str(root),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            timeout=_EXPORT_TIMEOUT_S,
            check=False,
        )
    if proc.returncode != 0 or not (root / "export.done").exists():
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"exporter exited {proc.returncode}; see {log}: {tail}")


def _daemon_user(pid: int) -> str | None:
    """The ``USER`` the daemon runs with: its shared-memory names carry it."""
    import psutil  # noqa: PLC0415

    with contextlib.suppress(psutil.Error, OSError):
        return psutil.Process(pid).environ().get("USER")
    return os.environ.get("USER")


def _listen_base(pid: int) -> int:
    import psutil  # noqa: PLC0415

    proc = psutil.Process(pid)
    listing = getattr(proc, "net_connections", None) or proc.connections
    ports = [c.laddr.port for c in listing(kind="tcp") if c.status == psutil.CONN_LISTEN]
    if not ports:
        raise RuntimeError(f"clio-core daemon pid {pid} listens on no TCP port")
    return min(ports)


def _client_config(store: Path, port: int, path: Path) -> Path:
    """CLIO's seed config for ``store`` on ``port``, written to ``path``.

    For a store whose own ``cte.yaml`` is gone: the seed layout names the same data
    files (a pre-created file bdev keeps its size), so the records are readable again.
    """
    from clio_agent.arc import clio_core_config as cc  # noqa: PLC0415 - cycle
    from clio_agent.arc import clio_core_indexer  # noqa: PLC0415 - cycle

    text = cc._DEFAULT_CTE_CONFIG_TEMPLATE.format(
        core_port=port,
        core_threads=cc.core_threads(),
        conf_dir=cc._cte_yaml_path(store / "conf"),
        file_tier=cc._cte_yaml_path(store / "storage.bin"),
        file_capacity=cc._default_cte_file_capacity(store),
        ram_budget=cc._DEFAULT_CTE_RAM_CAPACITY,
        metadata_log=cc._cte_yaml_path(store / "metadata.log"),
        indexer=clio_core_indexer.indexer_block(cc._cte_yaml_path(store / "indexer_index.log")),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _has_store_data(store: Path) -> bool:
    return any(next(store.glob(pattern), None) for pattern in ("metadata.log*", "storage.bin*"))


def _reconstruct_config(source: Path, pid: int, root: Path) -> Path:
    """A client config for a running daemon whose own config file is gone.

    The port is read from the running daemon; the rest is CLIO's seed layout for the
    store. Written in this home's import dir only.
    """
    port = _listen_base(pid)
    path = _client_config(source.parent, port, root / "source-client.yaml")
    logger.warning(
        "home import: the running daemon's config %s is gone; attaching with %s "
        "(port %s from the daemon)",
        source,
        path,
        port,
    )
    return path


def _private_copy(store: Path, root: Path, host: Path) -> Path | None:
    """A relocated copy of a stopped store, on its own port and small sizes.

    Returns ``None`` when the source daemon started again meanwhile (read it live).
    """
    from clio_agent.arc import home_runtime, runtime_ownership  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_effective_runtime import read_daemon_pid  # noqa: PLC0415
    from clio_agent.arc.clio_core_relocate import (  # noqa: PLC0415 - cycle
        _locations,
        inspect_store_location,
        relocate_store,
    )

    copy = root / "store"
    _retire_private_copy(root)
    shutil.copytree(store, copy)
    if read_daemon_pid(host)[1] is True:
        shutil.rmtree(copy, ignore_errors=True)
        return None
    config = copy / "cte.yaml"
    if not config.is_file():  # the config went with its home; the data files did not
        _client_config(copy, 0, config)
        logger.warning(
            "home import: %s had no config; rebuilt one for the private copy", store / "cte.yaml"
        )
    else:
        location = inspect_store_location(copy)
        if location is not None and location.moved_from is not None:
            relocate_store(copy, location.moved_from)
    here = copy.as_posix().rstrip("/").casefold()
    outside = [
        value
        for _key, value in _locations(config.read_text(encoding="utf-8"))
        if not value.replace("\\", "/").casefold().startswith(here)
    ]
    if outside:
        raise RuntimeError(f"the store copy still names paths outside it: {outside}")
    state = root / "runtime"
    state.mkdir(parents=True, exist_ok=True)
    for base in runtime_ownership.private_port_candidates(state):
        if runtime_ownership._block_free(base):
            runtime_ownership._write_port(config, base)
            break
    (copy / home_runtime._SIZED_MARKER).unlink(missing_ok=True)
    home_runtime._size_home_config(config)
    return copy


def _retire_private_copy(root: Path) -> None:
    """Stop the copy's daemon if it outlived the exporter, then delete the copy."""
    from clio_agent.arc.clio_core_effective_runtime import read_daemon_pid  # noqa: PLC0415

    state = root / "runtime"
    pid, live = read_daemon_pid(state)
    if live and pid is not None:
        import psutil  # noqa: PLC0415

        with contextlib.suppress(psutil.Error):
            proc = psutil.Process(pid)
            proc.kill()
            proc.wait(30)
    for path in (root / "store", state, root / "exporter-home"):
        shutil.rmtree(path, ignore_errors=True)


# ---- the export (child side) -----------------------------------------------------------


def _export(request: dict[str, Any]) -> None:
    from clio_agent.arc import storage  # noqa: PLC0415 - cycle

    store = storage.ClioCoreStore(config_path=request["config"], namespace="", kinds=())
    try:
        _collect(store, request)
    finally:
        storage.ClioCoreStore.drop_native_handles()
        storage.release_runtime_client()


def _bucket_rows(store: Any, tag: str, names: set[str], prefix: str) -> tuple[list[Any], set[str]]:
    from clio_agent.gact.session_index_columns import decode_bucket  # noqa: PLC0415

    rows: list[Any] = []
    unfilled: set[str] = set()
    for number in range(_BUCKETS):
        base_name, ext_name = f"{prefix}index/{number:02d}", f"{prefix}columns/{number:02d}"
        if base_name not in names:
            continue
        ext = store.get(tag, ext_name) if ext_name in names else None
        found, stale = decode_bucket(store.get(tag, base_name), ext)
        rows.extend(found)
        unfilled |= stale
    return rows, unfilled


def _owned_sessions(
    store: Any, tag: str, names: set[str], request: dict[str, Any]
) -> dict[str, tuple[str, Any, bool]]:
    """``{sid: (row record name, columns, unfilled)}`` of this home in the source."""
    namespace = request["namespace"]
    workspaces = set(request["workspaces"])
    transcripts = set(request["transcripts"])
    owned: dict[str, tuple[str, Any, bool]] = {}
    rows, unfilled = _bucket_rows(store, tag, names, namespace + "/")
    for row in rows:
        owned[row.id] = (f"{namespace}/row/{row.id}", row, row.id in unfilled)
    claims = {
        name[len("claimed/") :]: (store.get(tag, name) or b"").decode()
        for name in names
        if name.startswith("claimed/")
    }
    rows, unfilled = _bucket_rows(store, tag, names, "")
    legacy = {
        row.id: row
        for row in rows
        if row.id not in owned and claims.get(row.id, namespace) == namespace
    }
    mine = {
        sid for sid, row in legacy.items() if row.workspace_id in workspaces or sid in transcripts
    }
    grew = True
    while grew:
        grew = False
        for sid, row in legacy.items():
            parent = row.parent_session_id
            if sid not in mine and parent and (parent in mine or parent in owned):
                mine.add(sid)
                grew = True
    for sid in mine:
        owned[sid] = (f"row/{sid}", legacy[sid], sid in unfilled)
    return owned


def _belongs(
    store: Any, tag: str, kind: str, name: str, sessions: set[str], workspaces: set[str]
) -> bool:
    first = name.split("/", 1)[0]
    if kind in ("conversations", "response_feedback"):
        return first in sessions
    if kind == "invocations":
        from clio_agent.arc.schema import decode_invocation  # noqa: PLC0415

        body = store.get(tag, name)
        return body is not None and decode_invocation(body).session_id in sessions
    if kind == "segments":
        return name.split("__", 1)[0] in sessions
    if kind == "stream":
        return first in workspaces or first in _SHARED_WORKSPACES
    if kind in _MEMORY_KINDS and first == "project":
        scope = name.split("/", 2)[1] if name.count("/") >= 1 else ""
        return scope in workspaces or scope in _SHARED_WORKSPACES
    return True  # nothing names an owner: copied (a duplicate, never hidden)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _collect(store: Any, request: dict[str, Any]) -> None:
    pool = store._cte.PoolQuery.Dynamic()
    names: dict[str, set[str]] = {}
    for tag, blob in store._client.BlobQuery(".*", ".*", 0, pool):
        names.setdefault(tag, set()).add(blob)
    arc_ns = request["arc_namespace"]
    session_tag = _tag(arc_ns, _SESSION_KIND)
    owned = _owned_sessions(store, session_tag, names.get(session_tag, set()), request)
    sessions = set(owned) | set(request["transcripts"])
    workspaces = set(request["workspaces"])
    copied: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    lines = 0
    payload = Path(request["payload"])
    tmp = payload.with_name(payload.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for sid, (row_name, columns, unfilled) in sorted(owned.items()):
            raw = store.get_raw(session_tag, row_name)
            if raw is None:
                skipped["session_rows_missing"] += 1
                continue
            entry = {"session": sid, "b64": _b64(raw), "columns": asdict(columns)}
            out.write(json.dumps({**entry, "unfilled": unfilled}) + "\n")
            copied[_SESSION_KIND] += 1
            lines += 1
        for tag, blobs in sorted(names.items()):
            kind = tag.rsplit("/", 1)[-1]
            if tag == session_tag or kind in _DERIVED:
                skipped[kind] += sum(1 for name in blobs if not name.endswith(_COMPANION))
                continue
            for name in sorted(blobs):
                if name.endswith(_COMPANION):
                    continue
                if not _belongs(store, tag, kind, name, sessions, workspaces):
                    skipped[kind] += 1
                    continue
                raw = store.get_raw(tag, name)
                if raw is None:
                    continue
                entry = {"tag": tag, "name": name, "b64": _b64(raw)}
                if name + _COMPANION in blobs:
                    companion = store.get_raw(tag, name + _COMPANION)
                    if companion is not None:
                        entry["companion_b64"] = _b64(companion)
                        copied["search_companions"] += 1
                out.write(json.dumps(entry) + "\n")
                copied[kind] += 1
                lines += 1
    os.replace(tmp, payload)
    _write_json(
        Path(request["done"]),
        {
            "lines": lines,
            "sessions": len(owned),
            "copied": dict(copied),
            "skipped_other_homes": dict(skipped),
            "mode": request["mode"],
        },
    )


# ---- the import (after attach) ---------------------------------------------------------


def import_pending(store: Any) -> None:
    """Write the exported records into this home's store, once per process.

    Runs right after the first attach, before anything reads the store. Never raises:
    a failure is reported typed and the import resumes at the next start.
    """
    global _imported
    if os.environ.get(EXPORTER_ENV):
        return
    with _lock:
        if _imported:
            return
        _imported = True
        root = import_dir()
        if (root / "settled.json").exists() or not (root / "export.done").exists():
            return
        try:
            _import(store, root)
        except Exception as exc:  # noqa: BLE001 - forwards: typed status; resumed next start
            _set_status(state="failed", reason=HOME_IMPORT_FAILED, error=str(exc))
            logger.error(
                "home import failed (reason=%s): %s; it resumes at the next start",
                HOME_IMPORT_FAILED,
                exc,
            )


def _import(store: Any, root: Path) -> None:
    from clio_agent.arc import home_namespace, storage  # noqa: PLC0415 - cycle

    raw_store = storage.ClioCoreStore(config_path=store._config_path, namespace="", kinds=())
    arc_ns = _read_json(root / "request.json").get("arc_namespace", "")
    session_tag = _tag(arc_ns, _SESSION_KIND)
    prefix = home_namespace.session_record_prefix()
    state = _read_json(root / "status.json")
    start = int(state.get("done_lines", 0)) if state.get("state") == "importing" else 0
    created, existed = int(state.get("created", 0)), int(state.get("existed", 0))
    total = int(_read_json(root / "export.done").get("lines", 0))
    rows: list[tuple[Any, bool]] = []
    logger.warning(
        "home import: writing %d records into this home's store (from line %d, reason=%s)",
        total,
        start,
        HOME_IMPORT_PROGRESS,
    )
    _set_status(state="importing", done_lines=start, total_lines=total)
    with (root / "payload.jsonl").open(encoding="utf-8") as lines:
        for index, line in enumerate(lines):
            entry = json.loads(line)
            if "session" in entry:
                rows.append((entry["columns"], bool(entry.get("unfilled"))))
                tag, name = session_tag, f"{prefix}row/{entry['session']}"
            else:
                tag, name = entry["tag"], entry["name"]
            if index < start:
                continue
            if raw_store.put_if_absent(tag, name, base64.b64decode(entry["b64"])):
                created += 1
                if "companion_b64" in entry:
                    companion = base64.b64decode(entry["companion_b64"])
                    raw_store.put_at(tag, name + _COMPANION, companion, 0)
            else:
                existed += 1
            if (index + 1) % _PROGRESS_EVERY == 0:
                _set_status(done_lines=index + 1, created=created, existed=existed)
                logger.info(
                    "home import: %d/%d records (created=%d existed=%d reason=%s)",
                    index + 1,
                    total,
                    created,
                    existed,
                    HOME_IMPORT_PROGRESS,
                )
    _set_status(done_lines=total, created=created, existed=existed)
    merged = _merge_index(raw_store, session_tag, prefix, rows)
    if created:
        _reset_derived(raw_store, arc_ns)
    _settle("done", HOME_IMPORT_DONE, created=created, existed=existed, indexed=merged)
    (root / "payload.jsonl").unlink(missing_ok=True)
    logger.warning(
        "home import done (reason=%s): created=%d existed=%d sessions indexed=%d; the "
        "source store was not changed",
        HOME_IMPORT_DONE,
        created,
        existed,
        merged,
    )


def _merge_index(store: Any, tag: str, prefix: str, rows: list[tuple[Any, bool]]) -> int:
    """Add the imported sessions to this home's index buckets (rows already there win)."""
    from clio_agent.gact.session_index_columns import (  # noqa: PLC0415
        RecordColumns,
        decode_bucket,
        encode_bucket,
        extension_name,
    )
    from clio_agent.gact.session_records import bucket_of  # noqa: PLC0415

    grouped: dict[str, list[tuple[Any, bool]]] = {}
    for columns, row_unfilled in rows:
        row = RecordColumns(**columns)
        grouped.setdefault(bucket_of(row.id), []).append((row, row_unfilled))
    added = 0
    for bucket, imported in grouped.items():
        ext = extension_name(bucket)
        current, unfilled = decode_bucket(
            store.get(tag, prefix + bucket), store.get(tag, prefix + ext)
        )
        known = {row.id for row in current}
        fresh = [(row, stale) for row, stale in imported if row.id not in known]
        if not fresh:
            continue
        current.extend(row for row, _stale in fresh)
        unfilled |= {row.id for row, stale in fresh if stale}
        for name, body in encode_bucket(bucket, current, unfilled).items():
            store.put(tag, prefix + name, body)
        added += len(fresh)
    return added


def _reset_derived(store: Any, arc_ns: str) -> None:
    """Make every store migration run once more over the imported records."""
    lookup = _tag(arc_ns, "task_lookup")
    for name in store.query_names(lookup, r"meta\..*"):
        store.delete(lookup, name)
    store.delete(_tag(arc_ns, "artifact_index"), "m/marker")


def main(argv: list[str] | None = None) -> int:
    """``python -m clio_agent.arc.home_import export <request.json>`` (the exporter child)."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "export":
        print("usage: python -m clio_agent.arc.home_import export <request.json>", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _export(json.loads(Path(args[1]).read_text(encoding="utf-8")))
    return 0


__all__ = [
    "EXPORTER_ENV",
    "HOME_IMPORT_DONE",
    "HOME_IMPORT_FAILED",
    "HOME_IMPORT_NOTHING",
    "HOME_IMPORT_PROGRESS",
    "HOME_IMPORT_SOURCE_MISSING",
    "export_if_pending",
    "health_details",
    "import_dir",
    "import_pending",
    "status",
]


if __name__ == "__main__":
    raise SystemExit(main())
