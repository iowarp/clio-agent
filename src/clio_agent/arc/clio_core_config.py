"""clio-core CTE config generation + capacity resolution (owner module).

This module owns the default clio-core CTE configuration that ARC's clio-core backend
seeds under the OS data dir: a self-managed DRAM↔disk hierarchy (DRAM hot tier,
score 1.0; file cold tier, score 0.0). It was split out of
:mod:`clio_agent.arc.storage` so the capacity policy (how big the RAM hot tier is
allowed to grow) has a single home instead of being bolted onto the storage
god-file (owner-module discipline, iowarp/clio-agent#774/#890).

Why the RAM cap matters (#890): clio-core reads a tier ``capacity_limit`` of
``"0g"`` as *"default to 80% of total system DRAM"*. With the ram hot tier set to
``0g`` the context plane's working set was allowed to consume most of the machine
— a large, implicit slice of clio-agent's memory hunger, from one config line.

We therefore ship a **bounded, configurable default** (:data:`_DEFAULT_CTE_RAM_CAPACITY`
= ``"1GB"``) on the ram tier's ``capacity_limit`` (the field that actually triggers
spill — see below). A small hot tier is functionally safe, not merely desirable:
``tests/test_arc/test_clio_core_offload_spill.py`` proves that writing past a 2 MB ram
``capacity_limit`` physically spills cold blobs to the disk backing file and reads
them back byte-identically, and that test's own topology keeps the ram *bdev*
``capacity`` at ``"0g"`` while capping only the *tier* — so the tier
``capacity_limit`` is the real spill trigger and the safe place to bound resident
memory. We change only that field; the bdev ``capacity: "0g"`` is left as the
device ceiling, exactly matching the proven-safe offload topology.

The cap is configurable via the ``conf`` file→env→default resolver
(``arc.cte.ram_capacity`` / env ``CLIO_ARC_CTE_RAM_CAPACITY``), accepting values
like ``"1GB"`` / ``"512MB"`` / ``"0g"``. The value is format-validated fail-loud
(:func:`parse_capacity_bytes`) so a typo can never silently degrade back to the
``0g`` = 80%-DRAM footgun.

Regeneration semantics: :func:`default_cte_config_path` writes ``cte.yaml`` **once**
(only when absent) and never rewrites an existing file — an explicit user value is
always respected. A stale on-disk ``cte.yaml`` that still carries ``0g`` is therefore
*not* silently rewritten; instead the doctor probe
(:func:`clio_agent.runtime.clio_core_health.probe_clio_core_ram_cap`) reads the real file and
flags a ``0g`` ram tier as a warning with the exact remediation. That is the
least-surprising choice: no field of a user's config is ever mutated behind their
back.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml


def runtime_state_dir() -> Path:
    """Return the directory holding the clio-core runtime's host bookkeeping.

    This is where the connect-or-spawn lifecycle keeps its coordination state — the
    spawn lock (``clio-runtime.lock``), the daemon pidfile (``clio-runtime.pid``), the
    client refcount registry (``clio-runtime.clients/``), and the daemon log
    (``clio-runtime.log``). Default: ``~/.clio`` — the host-global location that lets
    every clio-agent process on the machine share ONE daemon.

    ``CLIO_RUNTIME_STATE_DIR`` overrides it (explicit selection, not a degrade): a
    process family that must NOT share the host daemon — the test suite's hermetic
    private daemon (``tests/_cte_isolation.py``), or a sandboxed deployment — points
    this at its own directory, and its spawn lock / pidfile / registry / last-one-out
    stop all move coherently with it. The directory is created if absent.

    Returns:
        The state directory path (guaranteed to exist).
    """
    override = os.environ.get("CLIO_RUNTIME_STATE_DIR", "").strip()
    state = Path(override).expanduser() if override else Path.home() / ".clio"
    state.mkdir(parents=True, exist_ok=True)
    return state

# Default clio-core CTE config: a self-managed DRAM↔disk hierarchy on the OS data
# dir. The DRAM tier (score 1.0) is the hot working set; the file tier (score 0.0)
# is the cold spill target. ``restart``/``metadata_log_path``/``transaction_log_capacity``
# are declared so the backend is ready for clio-core's cross-restart data recovery
# when it lands upstream (today that recovery is WIP, so durability rides the file
# trace + rebuild-on-reload — a permanent warm-up step, not a stopgap).
#
# The ram *tier* ``capacity_limit`` is ``{ram_capacity}`` (a bounded default, #890):
# it is the field that triggers spill to the cold file tier. The ram *bdev*
# ``capacity: "0g"`` stays as the device ceiling (max the ram bdev may grow to), which
# matches the proven-safe topology of ``tests/test_arc/test_clio_core_offload_spill.py``.
_DEFAULT_CTE_CONFIG_TEMPLATE = """\
runtime:
  num_threads: 4
  conf_dir: "{conf_dir}"
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_query: local
    pool_id: "301.0"
    bdev_type: ram
    capacity: "0g"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - path: "ram::cte_ram_tier"
        bdev_type: "ram"
        capacity_limit: "{ram_capacity}"
        score: 1.0
      - path: "{file_tier}"
        bdev_type: "file"
        capacity_limit: "{file_capacity}"
        score: 0.0
    dpe:
      dpe_type: "max_bw"
    performance:
      metadata_log_path: "{metadata_log}"
      transaction_log_capacity: "32MB"
"""

# The bounded default for the ram hot-tier ``capacity_limit`` (#890). Owner ruling
# (2026-07-12): 1GB AT MOST by default, user-configurable. A small hot tier is safe
# because capacity-forced offload to the clio-core file tier is proven byte-identical
# (tests/test_arc/test_clio_core_offload_spill.py); 1GB still
# comfortably holds a live context plane before spilling.
_DEFAULT_CTE_RAM_CAPACITY = "1GB"

# The path suffix identifying the ram hot tier inside a clio-core ``compose`` block.
_RAM_TIER_PATH_SUFFIX = "cte_ram_tier"

# Binary-multiplier unit table for clio-core capacity strings. Values are used only
# for validation + human display; clio-core owns the authoritative interpretation.
_CAP_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}

_CAP_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")


def parse_capacity_bytes(value: str) -> int:
    """Parse a clio-core capacity string (e.g. ``"1GB"``, ``"512MB"``, ``"0g"``) to bytes.

    Accepts an integer/float magnitude with an optional case-insensitive unit suffix
    (``b``/``k``/``kb``/``kib``/``m``/``mb``/``mib``/``g``/``gb``/``gib``/``t``/``tb``/
    ``tib``; bare number = bytes). Binary multipliers are used for the byte value, which
    drives validation + human display only — clio-core owns the authoritative reading.

    FAIL LOUD (#890): raises :class:`ValueError` on any unparseable magnitude or unknown
    unit so a typo (e.g. ``"2Gb!"`` or ``"tow gigs"``) cannot silently degrade to the
    ``0g`` = 80%-of-DRAM footgun.

    Args:
        value: The capacity string to parse.

    Returns:
        The capacity in bytes (binary multipliers).

    Raises:
        ValueError: If ``value`` is not ``<number>[<unit>]`` or the unit is unknown.
    """
    match = _CAP_RE.match(str(value))
    if match is None:
        raise ValueError(f"invalid CTE capacity {value!r}: expected e.g. '2GB', '512MB', or '0g'")
    magnitude, unit = match.group(1), match.group(2).lower()
    if unit not in _CAP_UNITS:
        raise ValueError(
            f"invalid CTE capacity unit in {value!r}: unknown unit {match.group(2)!r} "
            f"(expected one of b/kb/mb/gb/tb)"
        )
    return int(float(magnitude) * _CAP_UNITS[unit])


def _cte_yaml_path(path: Path) -> str:
    """Return a clio-core YAML-safe path string."""
    return path.as_posix()


def _default_cte_dir() -> Path:
    """Return the default clio-core CTE artifact directory."""
    from clio_agent import conf, paths  # noqa: PLC0415 - avoid import cycle

    configured = conf.resolve(
        "arc.cte.dir",
        env="CLIO_ARC_CTE_DIR",
        default="",
        cast=conf.as_str,
    ).strip()
    if configured:
        return Path(configured).expanduser()

    return paths.user_data_dir() / "cte"


def _default_cte_file_capacity() -> str:
    """Return the default clio-core CTE file-tier capacity."""
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle

    return (
        conf.resolve(
            "arc.cte.file_capacity",
            env="CLIO_ARC_CTE_FILE_CAPACITY",
            default="50GB",
            cast=conf.as_str,
        ).strip()
        or "50GB"
    )


def _default_cte_ram_capacity() -> str:
    """Return the default (bounded) clio-core CTE ram hot-tier ``capacity_limit``.

    Resolves ``arc.cte.ram_capacity`` / env ``CLIO_ARC_CTE_RAM_CAPACITY`` (file → env
    → :data:`_DEFAULT_CTE_RAM_CAPACITY`) and format-validates it fail-loud so a typo
    raises instead of silently becoming the ``0g`` = 80%-DRAM footgun (#890). An
    explicitly-configured value (including ``"0g"`` if a user truly wants 80% of DRAM)
    is respected; only the *generated* default changed from ``0g`` to a bounded cap.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle

    raw = (
        conf.resolve(
            "arc.cte.ram_capacity",
            env="CLIO_ARC_CTE_RAM_CAPACITY",
            default=_DEFAULT_CTE_RAM_CAPACITY,
            cast=conf.as_str,
        ).strip()
        or _DEFAULT_CTE_RAM_CAPACITY
    )
    # FAIL LOUD: a misconfigured cap must not silently pass through into the generated
    # config where clio-core would read a malformed value (or a bare ``0g``) as 80% DRAM.
    parse_capacity_bytes(raw)
    return raw


def default_cte_config_path() -> str:
    """Seed (if absent) and return the default clio-core CTE config path.

    Lives under the OS data dir (:func:`clio_agent.paths.user_data_dir` ``/cte``) and
    declares a DRAM hot tier + a file cold tier, so ARC's clio-core backend is a
    self-managed memory↔disk hierarchy by default — no LocalFS, no manual config.

    Written **once** (only when absent): an existing ``cte.yaml`` is never rewritten, so
    a user's explicit values survive untouched. A stale file still carrying ``0g`` is
    surfaced by the doctor (:func:`clio_agent.runtime.clio_core_health.probe_clio_core_ram_cap`)
    rather than silently mutated (#890).
    """
    cte_dir = _default_cte_dir()
    cte_dir.mkdir(parents=True, exist_ok=True)
    cfg = cte_dir / "cte.yaml"
    if not cfg.is_file():
        cfg.write_text(
            _DEFAULT_CTE_CONFIG_TEMPLATE.format(
                conf_dir=_cte_yaml_path(cte_dir / "conf"),
                file_tier=_cte_yaml_path(cte_dir / "storage.bin"),
                file_capacity=_default_cte_file_capacity(),
                ram_capacity=_default_cte_ram_capacity(),
                metadata_log=_cte_yaml_path(cte_dir / "metadata.log"),
            ),
            encoding="utf-8",
        )
    return str(cfg)


# --------------------------------------------------------------------------- #
# Doctor support: read the effective ram hot-tier cap WITHOUT seeding a file.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RamTierCap:
    """The effective clio-core ram hot-tier cap the ARC backend will run with.

    Attributes:
        config_path: The ``cte.yaml`` the runtime would use (may not exist yet).
        file_exists: Whether that config file is present on disk.
        cap: The ram tier ``capacity_limit`` string (from the file when present, else
            the bounded default the generator would write); ``None`` if the file exists
            but declares no ram tier.
        source: Where ``cap`` came from (``"file:<path>"`` or ``"generator-default"``).
        unbounded: True when ``cap`` parses to zero — the ``0g`` = 80%-DRAM footgun.
        parse_error: A fail-loud message when ``cap`` is present but unparseable, else
            ``None``.
    """

    config_path: str
    file_exists: bool
    cap: str | None
    source: str
    unbounded: bool
    parse_error: str | None


def _read_ram_cap_from_file(path: Path) -> str | None:
    """Return the ram hot-tier ``capacity_limit`` declared in ``path``, or ``None``.

    Reads the clio-core ``compose`` block and returns the ``capacity_limit`` of the
    storage tier whose path ends with :data:`_RAM_TIER_PATH_SUFFIX`. Returns ``None``
    on a missing/invalid file or when no ram tier is declared (never raises).
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, Mapping):
        return None
    for module in data.get("compose", []) or []:
        if not isinstance(module, Mapping):
            continue
        for tier in module.get("storage", []) or []:
            if not isinstance(tier, Mapping):
                continue
            if str(tier.get("path", "")).endswith(_RAM_TIER_PATH_SUFFIX):
                cap = tier.get("capacity_limit")
                return None if cap is None else str(cap)
    return None


def _resolve_config_path(env: Mapping[str, str]) -> Path:
    """Resolve the ``cte.yaml`` the clio-core backend would use, WITHOUT seeding it.

    Mirrors :func:`clio_agent.arc.storage.make_arc_store`'s selection order — explicit
    ``CLIO_ARC_STORE_CONFIG`` → per-workspace ``.clio/core/cte.yaml`` → the default CTE
    dir's ``cte.yaml`` — but never creates the default file (the doctor must observe
    reality, not manufacture it).
    """
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle

    explicit = (env.get("CLIO_ARC_STORE_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    workspace = paths.workspace_core_dir() / "cte.yaml"
    if workspace.is_file():
        return workspace
    return _default_cte_dir() / "cte.yaml"


def effective_ram_cap(*, env: Mapping[str, str]) -> RamTierCap:
    """Report the effective ram hot-tier cap for the doctor (read-only, no seeding).

    When the config file exists its declared ram cap is authoritative; when it does not
    yet exist the bounded default the generator *would* write is reported. A ``0g`` cap
    (or one parsing to zero) is marked :attr:`RamTierCap.unbounded`, and an unparseable
    cap is captured in :attr:`RamTierCap.parse_error` — neither is silently accepted.
    """
    path = _resolve_config_path(env)
    file_exists = path.is_file()
    if file_exists:
        cap = _read_ram_cap_from_file(path)
        source = f"file:{path}"
    else:
        cap = _default_cte_ram_capacity()
        source = "generator-default"

    parse_error: str | None = None
    unbounded = False
    if cap is not None:
        try:
            unbounded = parse_capacity_bytes(cap) == 0
        except ValueError as exc:
            parse_error = str(exc)
    return RamTierCap(
        config_path=str(path),
        file_exists=file_exists,
        cap=cap,
        source=source,
        unbounded=unbounded,
        parse_error=parse_error,
    )
