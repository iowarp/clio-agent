"""A moved CLIO data folder keeps using its own clio-core store (owner module).

clio-core records where its store lives as absolute paths in three places besides
the store's own files:

* ``cte.yaml``, CLIO's seed (written once): ``runtime.conf_dir``, the file tier
  ``storage[].path``, ``metadata_log_path`` and the indexer's ``index_log_path``;
* the pool log ``<conf_dir>/wal/pools.<n>.bin``: binary records ``u8 u8 | u64 pool
  major | u32 length, pool name | u32 length, chimod name | u32 length, params``, the
  params holding u64-length strings, among them the pool's compose YAML with the same
  paths;
* ``<conf_dir>/models/clio_bdev.<file tier path with / as _>_node0.0.0.yaml``: the file
  bdev's learned cost model, named after its path and naming it inside.

What iowarp-core 2.4.0 (the version CLIO pins) does with them, measured with ``strace``
on real stores (``tests/test_arc/test_clio_core_relocate_e2e.py`` keeps it checked): it
composes the pools ``cte.yaml`` declares, then replays the pool log. A pool the config
composes is taken from the config, and its log record is rewritten to match; a pool in
the log that the config does NOT compose is re-created from its logged params, at the
paths the log names. The pool log layout above is byte for byte the 2.3.x one; the
rewrite below is read back by 2.4.0 (a replayed pool opens the rewritten path). The
bdev allocation log 2.4.x adds (``storage.bin_node0.alloc_log``) is named after the bdev
file beside it and holds no path; the metadata and index logs hold none either.

A data folder moved to another path (a backup restored on another machine or under
another user name, a renamed home, a moved ``CLIO_AGENT_HOME``, a Windows profile move)
still names the OLD location in all three, and the clio-core started for it read and
wrote the store there: another install's store, or none.

:func:`ensure_store_location` runs before clio-core is spawned or attached
(:func:`clio_agent.arc.clio_core_config.default_cte_config_path`). It reads where the
config and the pool log name the store (:func:`inspect_store_location`, nothing
changed) and:

* **in place** (every location is this directory's, or one a person chose): nothing to
  do. A config naming its store in another directory that holds it, while this one
  holds none, is in place there (``cte_store_location_elsewhere``): it is used where it
  is, never refused.
* **moved**: the config names its runtime directory (``<old>/conf``) and its data under
  one other directory ``<old>`` laid out as CLIO's seed, and this directory holds the
  store (or nothing is left at ``<old>``). With no process using either location, every
  path under ``<old>`` in the model files (content and name), the pool log (record by
  record, its length fields fixed), the capacity journal and ``cte.yaml`` (last) is
  pointed here, logged ``cte_store_relocated``. Otherwise it is refused (below).
* **pool log only**: ``cte.yaml`` names this directory but the pool log still names an
  old seed directory that holds no store (left by the host-key migration before it
  rewrote the pool log). It is rewritten the same way; if something uses the store it
  is deferred to a later start (``cte_pool_log_relocation_deferred``), and a pool log
  of an unknown format is left as it is (``cte_pool_log_not_relocated``). Neither
  refuses: the config already names this directory.

Refusals (:class:`CteStoreLocationError`, before anything is changed) are only for a
moved store: ``cte_store_location_in_use`` (a live process holds the old or the new
location, the config's clio-core port is served, or the runtime records name a live
daemon) and ``cte_store_location_mismatch`` (the pool log cannot be rewritten, or the
config still names the old place after relocating). Each message gives the cause and
the fix. Every step is idempotent and ``cte.yaml`` is rewritten last, so a relocation
that is interrupted is finished by the next start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent import path_relocation

logger = logging.getLogger(__name__)

CTE_STORE_RELOCATED = "cte_store_relocated"
CTE_STORE_LOCATION_IN_USE = "cte_store_location_in_use"
CTE_STORE_LOCATION_MISMATCH = "cte_store_location_mismatch"
CTE_STORE_LOCATION_ELSEWHERE = "cte_store_location_elsewhere"
CTE_POOL_LOG_RELOCATION_DEFERRED = "cte_pool_log_relocation_deferred"
CTE_POOL_LOG_NOT_RELOCATED = "cte_pool_log_not_relocated"

#: ``key: value`` lines naming a store location in ``cte.yaml`` (values quoted).
_LOCATION = re.compile(
    r"^[ \t-]*(?P<key>conf_dir|path|metadata_log_path|index_log_path):[ \t]*"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\r\n]+?)(?P=quote)[ \t]*$",
    re.MULTILINE,
)
#: The same keys in the compose YAML the pool log embeds (bare values, followed by a
#: newline or by the record's binary fields), read without parsing the record layout.
_WAL_LOCATION = re.compile(
    rb"(?<![A-Za-z_])(conf_dir|path|metadata_log_path|index_log_path): ([^\x00-\x1f\"']+)"
)
#: The file names CLIO's seed gives each location, directly under the store directory.
_SEED_NAMES = {
    "conf_dir": re.compile(r"conf"),
    "path": re.compile(r"storage(-extra-\d{3})?\.bin"),
    "metadata_log_path": re.compile(r"metadata\.log"),
    "index_log_path": re.compile(r"indexer_index\.log"),
}
_JOURNAL = "cte-capacity.json"

_lock = threading.Lock()
_last: dict[str, Any] | None = None


class WalFormatError(ValueError):
    """The pool log does not parse as the record layout this module knows."""


class CteStoreLocationError(RuntimeError):
    """A moved store cannot be relocated; clio-core is not started on it.

    ``degradation_reason`` is the typed reason (``cte_store_location_in_use`` or
    ``cte_store_location_mismatch``) the clio-core attach row reports.
    """

    def __init__(self, reason: str, *, cte_dir: Path, recorded: str, cause: str, fix: str):
        self.degradation_reason = reason
        self.cte_dir = cte_dir
        self.config_path = cte_dir / "cte.yaml"
        self.recorded = recorded
        self.cause = cause
        self.fix = fix
        super().__init__(
            f"{reason}: CLIO will not start clio-core on the store in {cte_dir}: {cause}. "
            f"Fix: {fix}"
        )


@dataclass(frozen=True)
class StoreLocation:
    """Where a store's config and pool log say it lives, read without changing anything.

    Attributes:
        cte_dir: The directory holding ``cte.yaml``.
        moved_from: The old directory to relocate from (``None``: nothing to relocate).
        pool_log_only: Only the pool log names ``moved_from``; the config names here.
        naming: Files (relative to ``cte_dir``) that still name ``moved_from``.
        kept_at: The other directory the config names and that holds the store, used
            there as it is (``cte_store_location_elsewhere``).
    """

    cte_dir: Path
    moved_from: str | None = None
    pool_log_only: bool = False
    naming: tuple[str, ...] = ()
    kept_at: str | None = None

    @property
    def matches(self) -> bool:
        """Whether the store is used where it is recorded (nothing to relocate)."""
        return self.moved_from is None


# ---- the pool log -----------------------------------------------------------------------


def wal_records(data: bytes, name: str = "wal") -> list[tuple[bytes, list[bytes]]]:
    """Split a pool log into ``(fixed header, [pool name, chimod name, params])`` records.

    Raises:
        WalFormatError: The bytes do not parse as this record layout end to end.
    """
    records: list[tuple[bytes, list[bytes]]] = []
    at = 0
    while at < len(data):
        if at + 10 > len(data):
            raise WalFormatError(f"{name}: truncated record header at byte {at}")
        head = data[at : at + 10]
        at += 10
        fields: list[bytes] = []
        for _ in range(3):
            if at + 4 > len(data):
                raise WalFormatError(f"{name}: truncated length field at byte {at}")
            (size,) = struct.unpack_from("<I", data, at)
            at += 4
            if at + size > len(data):
                raise WalFormatError(f"{name}: field of {size} bytes overruns the file at {at}")
            fields.append(data[at : at + size])
            at += size
        records.append((head, fields))
    return records


def _enclosing_string(params: bytes, floor: int, hit: int, width: int) -> int | None:
    """The offset of the u64 length field of the UTF-8 string holding ``hit``, if any.

    Scanning down from ``hit``, the first offset whose u64 spans the hit and decodes is
    the real field: any offset in between reads text bytes as a huge length.
    """
    for start in range(hit - 8, floor - 1, -1):
        (size,) = struct.unpack_from("<Q", params, start)
        end = start + 8 + size
        if end < hit + width or end > len(params):
            continue
        try:
            params[start + 8 : end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return start
    return None


def _params_strings(params: bytes, needle: bytes, name: str) -> list[tuple[int, int]]:
    """``(length field offset, end)`` of each u64-length string holding ``needle``.

    Raises:
        WalFormatError: An occurrence is not inside a length-prefixed text string.
    """
    spans: list[tuple[int, int]] = []
    at = 0
    while (hit := params.find(needle, at)) >= 0:
        start = _enclosing_string(params, at, hit, len(needle))
        if start is None:
            raise WalFormatError(
                f"{name}: the path at params byte {hit} is not inside a length-prefixed "
                "text string; this pool log format cannot be rewritten"
            )
        (size,) = struct.unpack_from("<Q", params, start)
        spans.append((start, start + 8 + size))
        at = start + 8 + size
    return spans


def wal_texts(data: bytes, name: str = "wal") -> list[str]:
    """The compose YAML strings of a pool log (each params string naming a ``path:``)."""
    texts: list[str] = []
    for _head, (_pool, _chimod, params) in wal_records(data, name):
        for start, end in _params_strings(params, b"path:", name):
            texts.append(params[start + 8 : end].decode("utf-8"))
    return texts


def rewrite_wal(
    data: bytes, needle: bytes, transform: Callable[[str], str], name: str = "wal"
) -> bytes:
    """The pool log with ``transform`` applied to every string holding ``needle``.

    Each changed string gets its own length field fixed (u64 in the params, u32 for the
    pool and chimod names), so a new path need not be as long as the old one.

    Raises:
        WalFormatError: The log does not parse, or ``needle`` sits where it cannot be
            rewritten.
    """
    out = bytearray()
    for head, fields in wal_records(data, name):
        out += head
        for index, field in enumerate(fields):
            if index == 2:
                field = _rewrite_params(field, needle, transform, name)
            elif needle in field:
                try:
                    field = transform(field.decode("utf-8")).encode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WalFormatError(
                        f"{name}: a pool name naming the path is not text"
                    ) from exc
            out += struct.pack("<I", len(field)) + field
    rewritten = bytes(out)
    wal_records(rewritten, name)  # still parses end to end
    return rewritten


def _rewrite_params(
    params: bytes, needle: bytes, transform: Callable[[str], str], name: str
) -> bytes:
    out = bytearray()
    at = 0
    for start, end in _params_strings(params, needle, name):
        text = transform(params[start + 8 : end].decode("utf-8")).encode("utf-8")
        out += params[at:start] + struct.pack("<Q", len(text)) + text
        at = end
    return bytes(out + params[at:])


def mangled(path: str) -> str:
    """A path as clio-core spells it in a file bdev's model file name (``/`` as ``_``)."""
    return path.replace("/", "_")


# ---- reading where the store says it lives ----------------------------------------------


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _moved(path: str, old: str | None, here: str) -> str:
    """``path`` as it reads after relocating ``old`` to ``here``.

    A path under both (one directory nested in the other) belongs to the deeper one.
    """
    if old is None or not _under(path, old):
        return path
    if _under(path, here) and len(here) > len(old):
        return path
    return here + path[len(old) :]


def _relocator(old: str, here: str) -> Callable[[str], str]:
    """Rewrite every whole path under ``old`` in a text (only whole paths, never a
    sibling such as ``<old>-backup``)."""
    pattern = re.compile(r"(?<![^\s\"'])" + re.escape(old) + r"(?P<rest>/[^\s\"']*)?(?![^\s\"'])")

    def replace(match: re.Match[str]) -> str:
        return _moved(old + (match["rest"] or ""), old, here)

    return lambda text: pattern.sub(replace, text)


def _locations(text: str) -> list[tuple[str, str]]:
    return [(m["key"], m["value"].strip()) for m in _LOCATION.finditer(text)]


def _wal_locations(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """The store locations the pool logs name, read without parsing their records."""
    found: list[tuple[str, str]] = []
    for path in paths:
        for key, value in _WAL_LOCATION.findall(path.read_bytes()):
            found.append((key.decode(), value.decode("utf-8", "replace").strip()))
    return found


def _seed_dirs(locations: Iterable[tuple[str, str]]) -> set[str]:
    """The directories laid out as CLIO's seed: each holds at least two different
    locations under the file names the seed gives them.

    One location alone (a file tier a person put on another disk, even one named
    ``storage.bin``) does not make its directory a store directory.
    """
    keys: dict[str, set[str]] = {}
    for key, value in locations:
        pure = PurePosixPath(value)
        if pure.is_absolute() or re.match(r"^[A-Za-z]:/", value):
            if _SEED_NAMES[key].fullmatch(pure.name):
                keys.setdefault(str(pure.parent), set()).add(key)
    return {directory for directory, named in keys.items() if len(named) >= 2}


def _holds_store(directory: Path) -> bool:
    """Whether ``directory`` holds a store's data (checked by name; nothing is opened)."""
    return (
        (directory / "conf" / "wal").is_dir()
        or (directory / "storage.bin_node0").exists()
        or any(directory.glob("metadata.log*"))
    )


def _elsewhere(directory: str, here: str) -> bool:
    """A directory that is neither this one nor inside it (paths inside are in place)."""
    return directory != here and not _under(directory, here)


def _names(value: str, directory: str, here: str) -> bool:
    """Whether ``value`` is a path under ``directory`` that is not this store's own.

    This directory can lie inside ``directory`` (the host-key layout keeps the store in
    ``<data>/cte/hosts/<host>``, inside the old ``<data>/cte``): a path here is its own.
    """
    return _moved(value, directory, here) != value


def inspect_store_location(cte_dir: Path) -> StoreLocation | None:
    """Where ``cte_dir/cte.yaml`` and its pool log say the store lives (read only).

    Returns:
        ``None`` when there is no ``cte.yaml`` yet, else the :class:`StoreLocation`.
    """
    config = cte_dir / "cte.yaml"
    if not config.is_file():
        return None
    here = cte_dir.as_posix().rstrip("/")
    named = _locations(config.read_text(encoding="utf-8"))
    conf_dir = next((value for key, value in named if key == "conf_dir"), here + "/conf")
    # The config's store directory is the seed directory holding its runtime conf_dir
    # (``<dir>/conf``); a person's own data paths elsewhere are never a move.
    sources = [
        directory
        for directory in _seed_dirs(named)
        if _elsewhere(directory, here) and conf_dir == directory + "/conf"
    ]
    if sources:
        (old,) = sources
        if not _holds_store(cte_dir) and Path(old).exists():
            # This directory holds nothing of the store, which is where the config says.
            return StoreLocation(cte_dir, kept_at=old)
        wal = sorted(Path(here + "/conf").glob("wal/pools.*.bin"))
        naming = ["cte.yaml"] + [
            str(path.relative_to(cte_dir))
            for path in wal
            if any(_names(value, old, here) for _key, value in _wal_locations([path]))
        ]
        return StoreLocation(cte_dir, old, naming=tuple(naming))
    wal = sorted(Path(conf_dir).glob("wal/pools.*.bin"))
    configured = {value for _key, value in named}
    stale = [
        directory
        for directory in _seed_dirs(_wal_locations(wal))
        if _elsewhere(directory, here)
        and not any(_names(value, directory, here) for value in configured)
        and not _holds_store(Path(directory))
    ]
    if len(stale) != 1:
        # None, or several old places (nothing to choose between): the config names
        # this store, and a pool it composes is taken from the config.
        return StoreLocation(cte_dir)
    (old,) = stale
    naming = [
        str(path.relative_to(cte_dir)) if path.is_relative_to(cte_dir) else str(path)
        for path in wal
        if any(_names(value, old, here) for _key, value in _wal_locations([path]))
    ]
    return StoreLocation(cte_dir, old, pool_log_only=True, naming=tuple(naming))


# ---- relocating ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    path_relocation.atomic_write(path, data)


def relocate_store(cte_dir: Path, old: str, *, rewrite_pool_log: bool = True) -> list[str]:
    """Point every path under ``old`` in ``cte_dir``'s store here; ``cte.yaml`` last.

    The pool log rewrite is computed before anything is written, so a pool log that
    cannot be rewritten changes nothing. The caller has checked that nothing uses the
    store.

    Args:
        cte_dir: The directory holding the store now.
        old: The directory the store's files name.
        rewrite_pool_log: ``False`` leaves the pool log as it is (its format unknown).

    Returns:
        The files changed, relative to the conf directory or the store directory.

    Raises:
        WalFormatError: The pool log does not parse as the known layout (nothing was
            changed).
    """
    here = cte_dir.as_posix().rstrip("/")
    transform = _relocator(old, here)
    config = cte_dir / "cte.yaml"
    before = config.read_text(encoding="utf-8")
    conf_dir = next(
        (value for key, value in _locations(before) if key == "conf_dir"), here + "/conf"
    )
    conf = Path(_moved(conf_dir, old, here))
    logs: list[tuple[Path, bytes]] = []
    for path in sorted(conf.glob("wal/pools.*.bin")) if rewrite_pool_log else []:
        data = path.read_bytes()
        rewritten = rewrite_wal(data, old.encode("utf-8"), transform, path.name)
        if rewritten != data:
            logs.append((path, rewritten))
    changed = _relocate_models(conf, old, here, transform)
    for path, rewritten in logs:
        _atomic_write(path, rewritten)
        changed.append(f"wal/{path.name}")
    after = transform(before)
    journal = cte_dir / _JOURNAL
    if journal.is_file() and _relocate_journal(journal, before, after, transform):
        changed.append(_JOURNAL)
    if after != before:
        _atomic_write(config, after.encode("utf-8"))
        changed.append("cte.yaml")
    return changed


def _relocate_models(conf: Path, old: str, here: str, transform: Callable[[str], str]) -> list[str]:
    """Rename and rewrite the file bdev models named after a path under ``old``.

    One clio-core already wrote for the new path is kept (it is current), and the old
    one it supersedes is removed: both are clio-core's learned cost coefficients.
    """
    changed: list[str] = []
    old_name, new_name = mangled(old) + "_", mangled(here) + "_"
    for path in sorted(conf.glob("models/*")):
        text = path.read_bytes().decode("utf-8", "surrogateescape")
        name = path.name
        if old_name in name and not (len(here) > len(old) and new_name in name):
            name = name.replace(old_name, new_name, 1)
        relocated = transform(text)
        if name == path.name and relocated == text:
            continue
        target = path.with_name(name)
        if name != path.name and target.exists():
            path.unlink()
            changed.append(f"models/{path.name} (superseded by models/{name})")
            continue
        _atomic_write(target, relocated.encode("utf-8", "surrogateescape"))
        if name != path.name:
            path.unlink()
        changed.append(f"models/{name}")
    return changed


def _relocate_journal(
    journal: Path, before: str, after: str, transform: Callable[[str], str]
) -> bool:
    """Keep the capacity journal CLIO's after the config moves (``clio_core_growth``)."""

    def digest(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    record = json.loads(journal.read_text(encoding="utf-8"))
    original = json.dumps(record, sort_keys=True)
    if record.get("config_sha256") == digest(before):
        record["config_sha256"] = digest(after)
    pending = record.get("pending")
    if isinstance(pending, dict):
        for key in ("path", "config_text"):
            if isinstance(pending.get(key), str):
                pending[key] = transform(pending[key])
    if json.dumps(record, sort_keys=True) == original:
        return False
    _atomic_write(journal, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
    return True


def _relocate_runtime_record(cte_dir: Path, old: str) -> list[str]:
    """Point the stopped daemon's runtime record at the relocated config.

    The record (``clio-runtime.version`` beside the daemon pidfile) names the config the
    last daemon ran with, and connect-or-spawn reads that config for the daemon's port.
    No daemon is live here (:func:`_users`), so the record is stale; left naming the
    old ``cte.yaml``, the next start would read it from the old location.
    """
    from clio_agent.arc import clio_core_daemon_version  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415 - cycle

    state = runtime_state_dir()
    found = clio_core_daemon_version.read_daemon_version(state)  # None: absent or unreadable
    here = cte_dir.as_posix().rstrip("/")
    if found is None or not _under(Path(found.config_path).as_posix(), old):
        return []
    if _moved(Path(found.config_path).as_posix(), old, here) == found.config_path:
        return []
    path = clio_core_daemon_version._record_file(state)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["config_path"] = str(cte_dir / "cte.yaml")
    _atomic_write(path, json.dumps(record).encode("utf-8"))
    return [str(path)]


def _holders(directories: Iterable[Path]) -> list[str]:
    """Processes with a file under one of ``directories`` open.

    Processes this user may not inspect are not listed; the port and runtime-record
    checks in :func:`_users` still see a clio-core daemon among them.
    """
    import psutil  # noqa: PLC0415 - only on the relocation path

    prefixes = tuple(os.path.join(os.path.realpath(d), "") for d in directories if d.exists())
    if not prefixes:
        return []
    found: list[str] = []
    for process in psutil.process_iter(["name"]):
        try:
            files = process.open_files()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if any(item.path.startswith(prefixes) for item in files):
            found.append(f"pid {process.pid} ({process.info['name']})")
    return found


def _users(cte_dir: Path, old: str) -> list[str]:
    """What still uses the old or the new location: open files, the port, the records."""
    from clio_agent.arc.clio_core_config import runtime_state_dir  # noqa: PLC0415 - cycle
    from clio_agent.arc.clio_core_host_migration import (  # noqa: PLC0415 - cycle
        _config_port,
        _listening,
        _runtime_in_use,
    )

    users = _holders([cte_dir, Path(old)])
    port = _config_port(cte_dir / "cte.yaml")
    if port is not None and _listening(port):
        users.append(f"a process serving the config's clio-core port {port}")
    runtime = runtime_state_dir()
    if _runtime_in_use(runtime):
        users.append(f"a live clio-core daemon or client recorded in {runtime}")
    return users


def _pool_log_left(location: StoreLocation, reason: str, detail: str) -> None:
    """Log a pool log left naming its old place; the store starts on its config."""
    logger.warning(
        "reason=%s cte_dir=%s pool_log_names=%s files=%s (%s; cte.yaml names this store, "
        "and clio-core takes every pool it composes from cte.yaml)",
        reason,
        location.cte_dir,
        location.moved_from,
        ",".join(location.naming),
        detail,
    )


def ensure_store_location(cte_dir: Path) -> dict[str, Any] | None:
    """Relocate a moved store to ``cte_dir`` before clio-core starts, or refuse.

    Returns:
        ``None`` when the store is used where it is recorded (or there is no
        ``cte.yaml`` yet, or a pool-log-only relocation was deferred); else the
        ``cte_store_relocated`` record that was logged.

    Raises:
        CteStoreLocationError: A moved store cannot be relocated, or something still
            uses its old or new location. Never raised for a store in place.
    """
    location = inspect_store_location(cte_dir)
    if location is None or location.moved_from is None:
        return None
    old = location.moved_from
    users = _users(cte_dir, old)
    if users and location.pool_log_only:
        _pool_log_left(location, CTE_POOL_LOG_RELOCATION_DEFERRED, "; ".join(users))
        return None
    if users:
        raise CteStoreLocationError(
            CTE_STORE_LOCATION_IN_USE,
            cte_dir=cte_dir,
            recorded=old,
            cause=(
                f"the store was moved from {old}, and {'; '.join(users)} still uses the old "
                "or the new location"
            ),
            fix=(
                f"stop every CLIO and clio-core using {old} or {cte_dir} (or wait for them "
                "to exit), then start CLIO again; it relocates the store on that start"
            ),
        )
    try:
        changed = relocate_store(cte_dir, old)
    except WalFormatError as exc:
        if location.pool_log_only:
            _pool_log_left(location, CTE_POOL_LOG_NOT_RELOCATED, str(exc))
            return None
        raise CteStoreLocationError(
            CTE_STORE_LOCATION_MISMATCH,
            cte_dir=cte_dir,
            recorded=old,
            cause=f"the store was moved from {old}, but its pool log cannot be rewritten ({exc})",
            fix=f"restore the data folder to {old}, or move {cte_dir / 'cte.yaml'} aside to "
            "start a new, empty store here",
        ) from exc
    changed += _relocate_runtime_record(cte_dir, old)
    left = inspect_store_location(cte_dir)
    if left is not None and left.moved_from is not None and not left.pool_log_only:
        raise CteStoreLocationError(
            CTE_STORE_LOCATION_MISMATCH,
            cte_dir=cte_dir,
            recorded=old,
            cause=f"relocating from {old} left {', '.join(left.naming)} naming {left.moved_from}",
            fix=f"inspect {cte_dir / 'cte.yaml'}; restore the data folder to {old} to use it "
            "unchanged",
        )
    record = {"reason": CTE_STORE_RELOCATED, "from": old, "to": str(cte_dir), "files": changed}
    logger.warning(
        "reason=%s from=%s to=%s files=%s", CTE_STORE_RELOCATED, old, cte_dir, ",".join(changed)
    )
    global _last
    with _lock:
        _last = record
    path_relocation.record(record, log=False)  # for /v1/health's boot block
    return record


def last_relocation() -> dict[str, Any] | None:
    """The relocation this process made at start (for ``/v1/health``), if any."""
    with _lock:
        return dict(_last) if _last is not None else None
