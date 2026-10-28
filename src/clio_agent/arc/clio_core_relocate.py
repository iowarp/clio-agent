"""A moved CLIO data folder keeps using its own clio-core store (owner module).

clio-core records where its store lives as absolute paths in three places besides
the store's own files:

* ``cte.yaml``, CLIO's seed (written once): ``runtime.conf_dir``, the file tier
  ``storage[].path``, ``metadata_log_path`` and the indexer's ``index_log_path``;
* the pool write-ahead log ``<conf_dir>/wal/pools.<n>.bin``. On a restart clio-core
  recreates its pools from it. It is a sequence of binary records ``u8 u8 | u64 pool
  major | u32 length, pool name | u32 length, chimod name | u32 length, params``, and
  the params blob holds u64-length strings, among them the pool's compose YAML with
  the same paths;
* ``<conf_dir>/models/clio_bdev.<file tier path with / as _>_node0.0.0.yaml``: the
  file bdev's cost model, named after its path and naming it inside.

A data folder moved to another path (a backup restored on another machine or under
another user name, a renamed home, a moved ``CLIO_AGENT_HOME``, a Windows profile
move) still names the OLD location in all three. The clio-core started for it then
read and wrote the store at the old path: another install's store, or none.

:func:`ensure_store_location` runs before clio-core is spawned or attached
(:func:`clio_agent.arc.clio_core_config.default_cte_config_path`). It reads which store
directory the config and the pool log name (:func:`inspect_store_location`), and when
that is not this directory:

* one other directory laid out as CLIO's seed (at least two of ``<old>/conf``,
  ``<old>/metadata.log``, ``<old>/storage.bin``, ``<old>/indexer_index.log``) means the store was moved. With no
  process using the old or the new location, it is relocated in place: every path under
  ``<old>`` in the model files (content and name), the pool log (record by record, its
  length fields fixed), the capacity journal and ``cte.yaml`` (last) is pointed here,
  and ``cte_store_relocated`` is logged with both paths and kept for ``/v1/health``.
  Paths outside ``<old>`` (a person's own choice, such as a file tier on another disk)
  are left as they are, and nothing under ``<old>`` is opened.
* anything else is refused with :class:`CteStoreLocationError`, typed
  ``cte_store_location_in_use`` (a live process holds the old or the new location, the
  config's clio-core port is served, or the runtime records name a live daemon) or
  ``cte_store_location_mismatch`` (several store directories are named; the pool log
  names a location outside this directory that the config does not; the config names
  a store elsewhere while this directory holds none; the pool log cannot be parsed).
  The message gives the cause and the fix. The store is never silently used elsewhere.

Every step is idempotent and ``cte.yaml`` is rewritten last, so a relocation that is
interrupted is finished by the next start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

CTE_STORE_RELOCATED = "cte_store_relocated"
CTE_STORE_LOCATION_IN_USE = "cte_store_location_in_use"
CTE_STORE_LOCATION_MISMATCH = "cte_store_location_mismatch"

#: ``key: value`` lines naming a store location, in ``cte.yaml`` and in the compose
#: YAML clio-core keeps in its pool log (quoted in the first, bare in the second).
_LOCATION = re.compile(
    r"^[ \t-]*(?P<key>conf_dir|path|metadata_log_path|index_log_path):[ \t]*"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\r\n]+?)(?P=quote)[ \t]*$",
    re.MULTILINE,
)
#: The file names CLIO's seed gives each location, directly under the store directory.
_SEED_NAMES = {
    "conf_dir": re.compile(r"conf"),
    "path": re.compile(r"storage(-extra-\d{3})?\.bin"),
    "metadata_log_path": re.compile(r"metadata\.log"),
    "index_log_path": re.compile(r"indexer_index\.log"),
}
_WAL_LOCATION = re.compile(rb"path: ([^\x00-\x1f\"']+)")
_JOURNAL = "cte-capacity.json"

_lock = threading.Lock()
_last: dict[str, Any] | None = None


class WalFormatError(ValueError):
    """The pool log does not parse as the record layout this module knows."""


class CteStoreLocationError(RuntimeError):
    """The store's recorded location is not this directory and cannot be relocated.

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
        cte_dir: The directory holding ``cte.yaml`` (where the store is now).
        moved_from: The one other seed directory they name (the store was moved), else
            ``None``.
        naming: Files (relative to ``cte_dir``) that still name ``moved_from``.
        refusal: ``cte_store_location_mismatch`` when the names cannot be relocated.
        cause: Why, when refused.
        fix: What a person does about it, when refused.
    """

    cte_dir: Path
    moved_from: str | None = None
    naming: tuple[str, ...] = ()
    refusal: str | None = None
    cause: str = ""
    fix: str = ""

    @property
    def matches(self) -> bool:
        """Whether every recorded location is this directory's own (nothing to do)."""
        return self.moved_from is None and self.refusal is None


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


def _refused(cte_dir: Path, cause: str, fix: str, old: str | None = None) -> StoreLocation:
    return StoreLocation(cte_dir, old, refusal=CTE_STORE_LOCATION_MISMATCH, cause=cause, fix=fix)


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
    fix_seed = (
        f"to start a new, empty store here, move {config} aside; to use the store the "
        f"config names, set arc.cte.dir (CLIO_ARC_CTE_DIR) to that directory"
    )
    olds = _seed_dirs(named) - {here}
    if len(olds) > 1:
        return _refused(
            cte_dir, f"cte.yaml names several store directories {sorted(olds)}", fix_seed
        )
    old = next(iter(olds), None)
    conf_dir = next((value for key, value in named if key == "conf_dir"), here + "/conf")
    wal = sorted(Path(_moved(conf_dir, old, here)).glob("wal/pools.*.bin"))
    foreign = [
        path
        for path in wal
        if any(
            not _under(value.decode("utf-8", "replace"), here)
            for value in _WAL_LOCATION.findall(path.read_bytes())
        )
    ]
    logged: list[tuple[str, str]] = []
    for path in foreign:
        try:
            logged += [
                loc for text in wal_texts(path.read_bytes(), path.name) for loc in _locations(text)
            ]
        except WalFormatError as exc:
            return _refused(
                cte_dir,
                f"the clio-core pool log names another location and cannot be parsed ({exc})",
                fix_seed,
                old,
            )
    olds |= _seed_dirs(logged) - {here}
    if len(olds) > 1:
        return _refused(
            cte_dir,
            f"cte.yaml and the clio-core pool log name different store directories {sorted(olds)}",
            fix_seed,
        )
    old = next(iter(olds), None)
    configured = {_moved(value, old, here) for _key, value in named}
    stray = sorted({_moved(value, old, here) for _key, value in logged} - configured)
    stray = [path for path in stray if not _under(path, here)]
    if stray:
        return _refused(
            cte_dir,
            f"the clio-core pool log names {stray}, which cte.yaml does not name; clio-core "
            "would keep using it",
            "stop CLIO, then either restore cte.yaml to name that location or, to drop the "
            f"store's pool log and start from cte.yaml, move {Path(conf_dir) / 'wal'} aside",
            old,
        )
    if old is None:
        return StoreLocation(cte_dir)
    if not _holds_store(cte_dir) and _holds_store(Path(old)):
        return _refused(
            cte_dir,
            f"cte.yaml names the store in {old}, and {cte_dir} holds no store of its own",
            fix_seed,
            old,
        )
    naming = ["cte.yaml"] if any(_under(value, old) for _key, value in named) else []
    naming += [str(path.relative_to(cte_dir)) for path in foreign if path.is_relative_to(cte_dir)]
    return StoreLocation(cte_dir, old, tuple(naming))


# ---- relocating ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _relocate(cte_dir: Path, old: str) -> list[str]:
    """Point every path under ``old`` here; ``cte.yaml`` last. Returns what changed."""
    here = cte_dir.as_posix().rstrip("/")
    transform = _relocator(old, here)
    config = cte_dir / "cte.yaml"
    before = config.read_text(encoding="utf-8")
    conf_dir = next(
        (value for key, value in _locations(before) if key == "conf_dir"), here + "/conf"
    )
    conf = Path(_moved(conf_dir, old, here))
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
        _atomic_write(path.with_name(name), relocated.encode("utf-8", "surrogateescape"))
        if name != path.name:
            path.unlink()
        changed.append(f"models/{name}")
    for path in sorted(conf.glob("wal/pools.*.bin")):
        data = path.read_bytes()
        rewritten = rewrite_wal(data, old.encode("utf-8"), transform, path.name)
        if rewritten != data:
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


def ensure_store_location(cte_dir: Path) -> dict[str, Any] | None:
    """Relocate a moved store to ``cte_dir`` before clio-core starts, or refuse.

    Returns:
        ``None`` when the store's recorded location is already ``cte_dir`` (or there is
        no ``cte.yaml`` yet); else the ``cte_store_relocated`` record that was logged.

    Raises:
        CteStoreLocationError: The location cannot be relocated, or something still
            uses the old or the new location.
    """
    location = inspect_store_location(cte_dir)
    if location is None or location.matches:
        return None
    old = location.moved_from or ""
    if location.refusal is not None:
        raise CteStoreLocationError(
            location.refusal, cte_dir=cte_dir, recorded=old, cause=location.cause, fix=location.fix
        )
    users = _users(cte_dir, old)
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
        changed = _relocate(cte_dir, old) + _relocate_runtime_record(cte_dir, old)
    except WalFormatError as exc:
        raise CteStoreLocationError(
            CTE_STORE_LOCATION_MISMATCH,
            cte_dir=cte_dir,
            recorded=old,
            cause=f"the store was moved from {old}, but its pool log cannot be rewritten ({exc})",
            fix=f"restore the data folder to {old}, or move {cte_dir / 'cte.yaml'} aside to "
            "start a new, empty store here",
        ) from exc
    left = inspect_store_location(cte_dir)
    if left is not None and not left.matches:
        raise CteStoreLocationError(
            CTE_STORE_LOCATION_MISMATCH,
            cte_dir=cte_dir,
            recorded=old,
            cause=f"relocating from {old} left {left.cause or left.naming} naming another place",
            fix=f"inspect {cte_dir}; restore the data folder to {old} to use it unchanged",
        )
    record = {"reason": CTE_STORE_RELOCATED, "from": old, "to": str(cte_dir), "files": changed}
    logger.warning(
        "reason=%s from=%s to=%s files=%s", CTE_STORE_RELOCATED, old, cte_dir, ",".join(changed)
    )
    global _last
    with _lock:
        _last = record
    return record


def last_relocation() -> dict[str, Any] | None:
    """The relocation this process made at start (for ``/v1/health``), if any."""
    with _lock:
        return dict(_last) if _last is not None else None
