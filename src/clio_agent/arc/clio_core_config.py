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

We therefore ship a **hard MEMORY BUDGET** (:data:`_DEFAULT_CTE_RAM_CAPACITY`
= ``"1GB"``, #906 release gate — owner: "use 1GB of ram, and whatever you want
of <disk>"): ONE knob (``arc.cte.ram_capacity`` / ``CLIO_ARC_CTE_RAM_CAPACITY``)
derives the whole memory shape via :func:`derive_ram_shape` — the ram *bdev*
``capacity`` = the budget (the HARD allocation ceiling; clio-core's own default
of ``0g`` = up to 80% of DRAM is an HPC-compute-node default a desktop must
override), and the ram *tier* ``capacity_limit`` = budget/2 (the spill trigger,
leaving 2x eviction headroom inside the arena — the bounded-ceiling probe
proved spill works at 2x headroom and deadlocks rc=13 at ceiling == tier).
Values are format-validated fail-loud (:func:`parse_capacity_bytes`) so a typo
can never silently degrade back to the 80%-DRAM footgun.

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

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


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
# MEMORY BUDGET (#906, owner ruling 2026-07-13 — release-gating): a desktop
# clio-agent must NEVER be able to grow to clio-core's HPC default of 80% of
# system DRAM. ONE user-facing budget (``arc.cte.ram_capacity``) derives the
# whole memory shape: the ram *bdev* ``capacity`` = the budget (the HARD
# allocation ceiling of the DRAM arena — the engine cannot allocate past it),
# and the ram *tier* ``capacity_limit`` = budget/2 (the spill trigger, leaving
# proven 2x eviction headroom inside the arena; the bdev-ceiling probe showed
# spill works at 2x headroom and deadlocks rc=13 only at ceiling == tier).
# Disk stays effectively unbounded at the user-designated dir (arc.cte.dir).
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
    capacity: "{ram_budget}"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    restart: true
    storage:
      - path: "ram::cte_ram_tier"
        bdev_type: "ram"
        capacity_limit: "{ram_tier_limit}"
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

# The default MEMORY BUDGET (#890/#906): the hard DRAM bound for the clio-core
# storage arena. Owner ruling (2026-07-13): a desktop agent is told e.g. "use
# 1GB of ram, and whatever you want of <disk>" — the budget IS the ceiling.
_DEFAULT_CTE_RAM_CAPACITY = "1GB"


def derive_ram_shape(budget: str) -> tuple[str, str]:
    """Derive ``(bdev_ceiling, tier_limit)`` from the user's memory budget.

    The ceiling is the budget itself (hard bound); the tier limit is half of
    it, so eviction always has working headroom inside the arena (2x proven by
    the bounded-ceiling probe; ceiling == tier is the proven rc=13 deadlock).
    """
    budget_bytes = parse_capacity_bytes(budget)
    if budget_bytes <= 0:
        raise ValueError(f"memory budget must be > 0, got {budget!r}")
    tier_mb = max(1, budget_bytes // (2 * 1024 * 1024))
    return budget, f"{tier_mb}MB"

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
    """Return the default clio-core CTE file-tier capacity.

    The INTENDED semantic (owner ruling 2026-07-13, #906) is an UNBOUNDED
    final layer — ``capacity_limit`` bounds intermediate tiers only, because a
    final layer that fills makes writes fail (``PutBlob`` rc=13, proven live
    on the #893 gate) instead of spilling. clio-core cannot express that yet:
    ``core_config.cc`` rejects ``capacity_limit`` = 0 for non-ram tiers ("only
    'ram' tier supports 0"), so the default stays a LARGE bound until upstream
    supports an unbounded final layer. The boot check warns when the final
    layer is too small to absorb even one full hot-tier spill.
    """
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
    """Return the MEMORY BUDGET — the hard DRAM bound of the storage arena.

    Resolves ``arc.cte.ram_capacity`` / env ``CLIO_ARC_CTE_RAM_CAPACITY`` (file → env
    → :data:`_DEFAULT_CTE_RAM_CAPACITY`) and format-validates it fail-loud so a typo
    raises instead of silently becoming the ``0g`` = 80%-DRAM footgun (#890/#906).
    The generated config derives its whole memory shape from this ONE knob via
    :func:`derive_ram_shape`: bdev ceiling = budget, tier limit = budget/2.
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
    # FAIL LOUD: a misconfigured budget must not silently pass through into the
    # generated config where clio-core would read a malformed value (or a bare
    # ``0g``) as 80% DRAM.
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
        ram_budget, ram_tier_limit = derive_ram_shape(_default_cte_ram_capacity())
        cfg.write_text(
            _DEFAULT_CTE_CONFIG_TEMPLATE.format(
                conf_dir=_cte_yaml_path(cte_dir / "conf"),
                file_tier=_cte_yaml_path(cte_dir / "storage.bin"),
                file_capacity=_default_cte_file_capacity(),
                ram_budget=ram_budget,
                ram_tier_limit=ram_tier_limit,
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
        bdev_capacity: The ram *bdev* ``capacity`` string, surfaced for visibility
            (#906 — the 12.3 GiB incident's stale config carried ``0g`` in TWO places).
            ``"0g"`` here is the device ceiling and is the proven-safe generated
            default WHEN the tier ``capacity_limit`` is bounded (the tier limit is the
            spill trigger — see the module docstring); it is reported, not judged.
    """

    config_path: str
    file_exists: bool
    cap: str | None
    source: str
    unbounded: bool
    parse_error: str | None
    bdev_capacity: str | None = None
    final_tier_capacity: str | None = None


def _read_ram_caps_from_file(path: Path) -> tuple[str | None, str | None, str | None]:
    """Return ``(tier_capacity_limit, ram_bdev_capacity, final_tier_capacity)``.

    Reads the clio-core ``compose`` block: the ram hot tier's ``capacity_limit``
    (the storage tier whose path ends with :data:`_RAM_TIER_PATH_SUFFIX` — the
    spill trigger, #890), the ram bdev module's ``capacity`` (the device
    ceiling, #906), and the FINAL tier's ``capacity_limit`` (lowest ``score``
    — the layer that must be unbounded per the topology rule; a bounded final
    layer fills and fails writes, proven live on the #893 gate). Any is
    ``None`` when absent; all are ``None`` on a missing/invalid file (never
    raises).
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, None, None
    if not isinstance(data, Mapping):
        return None, None, None
    tier_cap: str | None = None
    bdev_cap: str | None = None
    final_cap: str | None = None
    final_score: float | None = None
    for module in data.get("compose", []) or []:
        if not isinstance(module, Mapping):
            continue
        if str(module.get("bdev_type", "")).strip().lower() == "ram":
            capacity = module.get("capacity")
            if capacity is not None:
                bdev_cap = str(capacity)
        for tier in module.get("storage", []) or []:
            if not isinstance(tier, Mapping):
                continue
            if str(tier.get("path", "")).endswith(_RAM_TIER_PATH_SUFFIX):
                cap = tier.get("capacity_limit")
                if cap is not None:
                    tier_cap = str(cap)
            try:
                score = float(tier.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if final_score is None or score < final_score:
                final_score = score
                cap = tier.get("capacity_limit")
                final_cap = None if cap is None else str(cap)
    return tier_cap, bdev_cap, final_cap


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


def effective_ram_cap(
    *, env: Mapping[str, str], config_path: str | Path | None = None
) -> RamTierCap:
    """Report the effective ram hot-tier cap for the doctor (read-only, no seeding).

    When the config file exists its declared ram cap is authoritative; when it does not
    yet exist the bounded default the generator *would* write is reported. A ``0g`` cap
    (or one parsing to zero) is marked :attr:`RamTierCap.unbounded`, and an unparseable
    cap is captured in :attr:`RamTierCap.parse_error` — neither is silently accepted.
    The ram bdev ``capacity`` rides along in :attr:`RamTierCap.bdev_capacity` (#906).

    Args:
        env: Environment mapping driving the config-path resolution.
        config_path: The exact ``cte.yaml`` to inspect, when the caller already
            resolved it (the boot check passes the file the store will actually
            use — which may come from a conf-file setting the env-only resolver
            cannot see). Defaults to the env-based resolution.
    """
    path = Path(config_path).expanduser() if config_path else _resolve_config_path(env)
    file_exists = path.is_file()
    bdev_cap: str | None = None
    final_cap: str | None = None
    if file_exists:
        cap, bdev_cap, final_cap = _read_ram_caps_from_file(path)
        source = f"file:{path}"
    else:
        bdev_cap, cap = derive_ram_shape(_default_cte_ram_capacity())
        final_cap = _default_cte_file_capacity()
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
        bdev_capacity=bdev_cap,
        final_tier_capacity=final_cap,
    )


# --------------------------------------------------------------------------- #
# Boot-time environment conformance (#906): check the EFFECTIVE config loudly.
# --------------------------------------------------------------------------- #

# Typed reason for an unbounded/unparseable EFFECTIVE ram cap at backend init.
CLIO_CORE_RAM_UNCAPPED = "clio_core_ram_uncapped"

# Typed reason for a tier-topology violation (#893 live-gate finding; owner
# rule 2026-07-13): capacity limits bound INTERMEDIATE tiers only — the ram
# bdev device ceiling and the FINAL tier must be unbounded ("0g"). A bounded
# ceiling/final layer means the hierarchy fills and writes fail (PutBlob
# rc=13) instead of spilling — proven live on the #893 gate's SF leg.
CLIO_CORE_TIER_TOPOLOGY = "clio_core_tier_topology"

_BOOT_REMEDIATION = (
    "set arc.cte.ram_capacity / CLIO_ARC_CTE_RAM_CAPACITY and regenerate, or edit "
    "capacity_limit under storage[cte_ram_tier] in the config file"
)


def boot_check_ram_cap(config_path: str | Path, *, env: Mapping[str, str]) -> RamTierCap:
    """Check the EFFECTIVE ram cap of the config the store will use, loudly (#906).

    The #890 bounded default only governs *generated* configs; a stale on-disk
    ``cte.yaml`` (deliberately never rewritten) kept this box's daemon unbounded
    for weeks with only an on-demand doctor flag. Tests can never own this —
    they are correctly isolated from the real environment — so conformance is a
    RUNTIME responsibility: this runs at clio-core backend init, inspects the
    exact file handed to the store, and emits ONE typed WARNING
    (:data:`CLIO_CORE_RAM_UNCAPPED`) when the effective cap is unbounded,
    unparseable, or missing. Read-only; never blocks boot.

    Args:
        config_path: The resolved ``cte.yaml`` the store is about to load.
        env: Environment mapping (forwarded for provenance only).

    Returns:
        The inspected :class:`RamTierCap` (callers may record it further).
    """
    cap = effective_ram_cap(env=env, config_path=config_path)
    if cap.parse_error is not None:
        problem = f"ram capacity_limit {cap.cap!r} is unparseable ({cap.parse_error})"
    elif cap.cap is None and cap.file_exists:
        problem = "config declares no ram hot-tier capacity_limit"
    elif cap.unbounded:
        problem = f"ram capacity_limit {cap.cap!r} = 80% of total system DRAM"
    else:
        problem = None
    if problem is not None:
        logger.warning(
            "reason=%s config=%s problem=%s bdev_capacity=%s remediation=%s (#906)",
            CLIO_CORE_RAM_UNCAPPED,
            cap.config_path,
            problem,
            cap.bdev_capacity,
            _BOOT_REMEDIATION,
        )

    # Topology rules (#893/#906, owner ruling — release-gating memory bound):
    # the ram bdev CEILING must be BOUNDED (an unbounded ceiling lets a desktop
    # agent grow to clio-core's HPC default of 80% of system DRAM) and must
    # leave spill headroom above the tier limit (>= 2x proven by the
    # bounded-ceiling probe; ceiling == tier is the proven rc=13 deadlock).
    # The FINAL tier must at least absorb one full hot-tier spill. (The
    # intended final-layer rule is unbounded, but clio-core rejects
    # capacity_limit=0 on non-ram tiers — see _default_cte_file_capacity.)
    topology = []
    tier_ok = cap.cap is not None and not cap.unbounded and cap.parse_error is None
    if cap.bdev_capacity is not None and not _is_bounded(cap.bdev_capacity):
        topology.append(
            f"ram bdev capacity {cap.bdev_capacity!r} is UNBOUNDED — clio-core reads it "
            "as up to 80% of system DRAM; a desktop install must set the memory-budget "
            "ceiling (arc.cte.ram_capacity derives it)"
        )
    elif (
        _is_bounded(cap.bdev_capacity)
        and tier_ok
        and cap.cap is not None
        and parse_capacity_bytes(cap.bdev_capacity or "0") < 2 * parse_capacity_bytes(cap.cap)
    ):
        topology.append(
            f"ram bdev ceiling {cap.bdev_capacity!r} is < 2x the tier limit {cap.cap!r} — "
            "insufficient spill headroom (ceiling == tier is the proven rc=13 deadlock)"
        )
    if (
        _is_bounded(cap.final_tier_capacity)
        and tier_ok
        and cap.cap is not None
        and parse_capacity_bytes(cap.final_tier_capacity or "0")
        <= parse_capacity_bytes(cap.cap)
    ):
        topology.append(
            f"final tier capacity_limit {cap.final_tier_capacity!r} is <= the ram tier "
            f"cap {cap.cap!r} — the hierarchy cannot absorb one full hot-tier spill "
            "and writes will fail with PutBlob rc=13 when it fills"
        )
    if topology:
        logger.warning(
            "reason=%s config=%s problems=%s (#906/#893)",
            CLIO_CORE_TIER_TOPOLOGY,
            cap.config_path,
            "; ".join(topology),
        )
    return cap


def _is_bounded(value: str | None) -> bool:
    """True when ``value`` is a parseable, NON-zero capacity (a real bound)."""
    if value is None:
        return False
    try:
        return parse_capacity_bytes(value) > 0
    except ValueError:
        return False
