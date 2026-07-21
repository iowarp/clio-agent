"""``clio doctor --gc`` — the manual/recovery disk reclamation path (iowarp/clio-agent#1001).

The steady-state bound is :mod:`clio_agent.tools.mcp_cache` (evict-on-boot for the MCP uv
cache). This module is the **FALLBACK**: a one-shot deep reclamation the operator runs when
caches have already grown — after a crash, a long grind, or an old install predating the
boot bound. It is deliberately conservative and, above all, **refuses to run while any clio
server / daemon / MCP process is alive** (astral-sh/uv#11694: pruning a uv cache under a
live spawner corrupts ephemeral envs). The refusal carries a typed reason and the live
peers, so it is never a silent no-op.

When it may run (no live peers) it reclaims, each with a typed row in the report:

1. the clio-owned MCP uv cache beyond its configured bounds (delegates to
   :func:`clio_agent.tools.mcp_cache.prune_uv_cache`);
2. clio-kit's ``mcp-environments`` keeping the NEWEST environment per server (parsed
   defensively — an unrecognized layout is SKIPPED with a typed reason, never wrong-deleted);
3. ``uv cache prune`` on the ambient uv cache, clio-kit's private cache, and the MCP uv
   cache (subprocess; tolerates uv absent / a missing dir);
4. stale pytest basetemp trees under the configured temp roots older than N days.

``--dry-run`` computes the exact plan (paths, reasons, bytes) and mutates NOTHING.

The live-process probe :func:`live_peer_clio_processes` is shared with the boot prune in
:mod:`clio_agent.tools.mcp_cache`: both must answer "is a PEER clio process alive?" (a peer
being any clio process that is not this process or one of its own descendants).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent import conf
from clio_agent.runtime import trace
from clio_agent.tools import mcp_cache

__all__ = [
    "ProcRef",
    "GCLocationResult",
    "GCReport",
    "live_peer_clio_processes",
    "run_gc",
]

# Substrings that positively identify a LIVE clio process by executable name or command
# line. Deliberately PRECISE invocation tokens — a module path (``clio_agent.gact``), a
# console script (``clio-agent-gact``), the serve verb, the daemon, and the clio-kit MCP
# fleet — NOT the bare ``clio-agent`` string, which would also match any unrelated shell
# whose working directory happens to sit under a ``clio-agent`` checkout (the repo path is
# ``clio-agent-s1``). Over-matching here would make ``clio doctor --gc`` permanently refuse
# on a developer box; these tokens fire only on an actual clio invocation.
_CLIO_MARKERS = (
    "clio_agent.gact",  # gact server module (the MCP spawner)
    "clio-agent-gact",  # gact server console script
    "clio-agent serve",  # front-door serve verb (console script form)
    "clio_agent.ui.cli serve",  # front-door serve verb (module form)
    "clio_run",  # clio-core daemon
    "clio-kit",  # clio-kit MCP fleet launcher/children
    "clio_kit",
)

# Default age ceiling for stale pytest basetemp trees under the temp roots.
_DEFAULT_TEMP_MAX_AGE_DAYS = 3.0

# pytest basetemp directory name patterns (``pytest-of-<user>/``, ``pytest-<n>/``, and the
# ``--basetemp`` dirs the CLAUDE.md workflow uses under ``D:/t``). Conservative: only names
# matching these are ever considered, so an unrelated temp dir is never deleted.
_PYTEST_TEMP_GLOBS = ("pytest-of-*", "pytest-*", "garbage-*")


@dataclass(frozen=True)
class ProcRef:
    """A live clio process the GC treats as a blocking peer."""

    pid: int
    name: str
    marker: str

    def __str__(self) -> str:
        return f"{self.name}(pid={self.pid}; {self.marker})"


# --------------------------------------------------------------------------- #
# Live-process probe (shared by the boot prune and the gc refusal)
# --------------------------------------------------------------------------- #


def _own_pids() -> set[int]:
    """This process plus every descendant (best-effort via psutil)."""
    own = {os.getpid()}
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return own
    try:
        me = psutil.Process()
        for child in me.children(recursive=True):
            own.add(child.pid)
    except Exception:  # noqa: BLE001,S110 - psutil raced/denied; self-pid is still excluded, which is sufficient
        pass
    return own


def _daemon_pid() -> int | None:
    """The shared clio-core daemon pid from its pidfile, if the pid is alive."""
    try:
        from clio_agent.arc import storage  # noqa: PLC0415

        parts = storage._daemon_pidfile().read_text(encoding="utf-8").split()
    except Exception:  # noqa: BLE001 - no pidfile / import surface varies; absent daemon is the safe default
        return None
    if not parts:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    try:
        import psutil  # noqa: PLC0415

        return pid if psutil.pid_exists(pid) else None
    except ImportError:
        return pid


def live_peer_clio_processes(*, exclude_pids: Iterable[int] | None = None) -> list[ProcRef]:
    """Return the live clio processes that are NOT this process or its descendants.

    A peer is any process whose executable name or command line carries a clio marker
    (:data:`_CLIO_MARKERS`), plus the shared clio-core daemon identified via its pidfile
    (whose python name may be generic). The returned list is empty exactly when this
    process owns the field — the condition under which a boot prune / gc is safe.

    Args:
        exclude_pids: PIDs to treat as "self" (defaults to this process + its descendants).

    Returns:
        One :class:`ProcRef` per live peer, sorted by pid.
    """
    exclude = set(exclude_pids) if exclude_pids is not None else _own_pids()
    peers: dict[int, ProcRef] = {}

    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        # No psutil: fall back to the daemon pidfile alone (best available signal).
        daemon = _daemon_pid()
        if daemon is not None and daemon not in exclude:
            peers[daemon] = ProcRef(pid=daemon, name="clio_run", marker="clio-core-daemon-pidfile")
        return sorted(peers.values(), key=lambda p: p.pid)

    daemon = _daemon_pid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(proc.info["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid in exclude:
            continue
        name = str(proc.info.get("name") or "")
        try:
            cmdline = " ".join(proc.info.get("cmdline") or ())
        except (TypeError, ValueError):
            cmdline = ""
        haystack = f"{name} {cmdline}".lower()
        marker = next((m for m in _CLIO_MARKERS if m in haystack), None)
        if marker is None and daemon is not None and pid == daemon:
            marker = "clio-core-daemon-pidfile"
        if marker is not None:
            peers[pid] = ProcRef(pid=pid, name=name or "python", marker=marker)

    if daemon is not None and daemon not in exclude and daemon not in peers:
        peers[daemon] = ProcRef(pid=daemon, name="clio_run", marker="clio-core-daemon-pidfile")
    return sorted(peers.values(), key=lambda p: p.pid)


# --------------------------------------------------------------------------- #
# GC report types
# --------------------------------------------------------------------------- #


@dataclass
class GCLocationResult:
    """Reclamation outcome for one location."""

    location: str
    path: str
    reason: str
    bytes_freed: int | None = 0
    kept: int = 0
    removed: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "location": self.location,
            "path": self.path,
            "reason": self.reason,
            "bytes_freed": self.bytes_freed,
            "kept": self.kept,
            "removed": self.removed,
            "detail": self.detail,
        }


@dataclass
class GCReport:
    """The full ``clio doctor --gc`` outcome."""

    dry_run: bool
    refused: bool = False
    refusal_reason: str = ""
    peers: list[ProcRef] = field(default_factory=list)
    locations: list[GCLocationResult] = field(default_factory=list)

    @property
    def total_bytes_freed(self) -> int:
        return sum(loc.bytes_freed or 0 for loc in self.locations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "peers": [str(p) for p in self.peers],
            "total_bytes_freed": self.total_bytes_freed,
            "locations": [loc.to_dict() for loc in self.locations],
        }


# --------------------------------------------------------------------------- #
# Location prunes
# --------------------------------------------------------------------------- #


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                continue
    return total


def _clio_kit_cache_dir(env: Mapping[str, str]) -> Path | None:
    """Resolve clio-kit's cache root defensively (config/env → platformdirs → ~/.cache)."""
    override = (env.get("CLIO_KIT_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    candidates: list[Path] = []
    try:
        import platformdirs  # noqa: PLC0415

        candidates.append(Path(platformdirs.user_cache_dir("clio-kit", appauthor=False)))
    except ImportError:
        pass
    candidates.append(Path.home() / ".cache" / "clio-kit")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0] if candidates else None


def _prune_clio_kit_environments(kit_cache: Path | None, *, dry_run: bool) -> GCLocationResult:
    """Keep the NEWEST ``mcp-environments`` entry per server; evict the rest.

    Layout (defensive): ``<kit_cache>/mcp-environments/<server>-<hash>/``. The server key
    is everything before the LAST ``-``; the newest per key (by mtime) is kept. If the
    directory is absent or its entries do not match the expected ``<name>-<hash>`` shape,
    the location is SKIPPED with a typed reason — never a wrong delete.
    """
    if kit_cache is None:
        return GCLocationResult(
            location="clio-kit mcp-environments",
            path="",
            reason="clio_kit_cache_not_found",
            bytes_freed=None,
            detail="clio-kit cache root could not be resolved; skipped.",
        )
    env_root = kit_cache / "mcp-environments"
    if not env_root.is_dir():
        return GCLocationResult(
            location="clio-kit mcp-environments",
            path=str(env_root),
            reason="clio_kit_layout_absent",
            bytes_freed=None,
            detail="no mcp-environments directory; nothing to prune.",
        )

    entries = [p for p in env_root.iterdir() if p.is_dir()]
    if not entries or not all("-" in p.name for p in entries):
        # Unrecognized layout — refuse to guess which is which.
        trace.event(
            "DISK-GC",
            "clio_kit_layout_unrecognized dir=%s entries=%d — skipped (no wrong delete)",
            env_root,
            len(entries),
        )
        return GCLocationResult(
            location="clio-kit mcp-environments",
            path=str(env_root),
            reason="clio_kit_layout_unrecognized",
            bytes_freed=None,
            detail=(
                f"{len(entries)} entries do not match <server>-<hash>; skipped to avoid a "
                "wrong delete."
            ),
        )

    by_server: dict[str, list[Path]] = {}
    for p in entries:
        server = p.name.rsplit("-", 1)[0]
        by_server.setdefault(server, []).append(p)

    freed = 0
    removed = 0
    kept = 0
    for _server, paths in by_server.items():
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # newest first
        kept += 1  # keep paths[0]
        for victim in paths[1:]:
            size = _dir_size(victim)
            freed += size
            removed += 1
            if not dry_run:
                shutil.rmtree(victim, ignore_errors=True)
            trace.event(
                "DISK-GC",
                "%sclio_kit_env_evicted path=%s freed=%d",
                "would-" if dry_run else "",
                victim,
                size,
            )

    return GCLocationResult(
        location="clio-kit mcp-environments",
        path=str(env_root),
        reason="kept_newest_per_server",
        bytes_freed=freed,
        kept=kept,
        removed=removed,
        detail=f"kept newest of {len(by_server)} server(s).",
    )


_UV_FREED_RE = re.compile(r"\(([\d.]+)\s*([KMGT]?i?B)\)")
_UV_UNIT = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
}


def _parse_uv_freed(text: str) -> int | None:
    """Best-effort parse of ``uv cache prune``'s ``(X MiB)`` freed figure."""
    match = _UV_FREED_RE.search(text)
    if match is None:
        return None
    try:
        magnitude = float(match.group(1))
    except ValueError:
        return None
    return int(magnitude * _UV_UNIT.get(match.group(2), 1))


def _uv_cache_prune(
    label: str, cache_dir: Path, *, env: Mapping[str, str], dry_run: bool
) -> GCLocationResult:
    """Run ``uv cache prune`` against ``cache_dir`` (subprocess; tolerant of uv absent)."""
    if not cache_dir.is_dir():
        return GCLocationResult(
            location=f"uv cache prune ({label})",
            path=str(cache_dir),
            reason="cache_dir_absent",
            bytes_freed=None,
            detail="cache directory does not exist; skipped.",
        )
    if shutil.which("uv") is None:
        return GCLocationResult(
            location=f"uv cache prune ({label})",
            path=str(cache_dir),
            reason="uv_not_on_path",
            bytes_freed=None,
            detail="uv is not on PATH; skipped.",
        )
    if dry_run:
        return GCLocationResult(
            location=f"uv cache prune ({label})",
            path=str(cache_dir),
            reason="dry_run_planned",
            bytes_freed=None,
            detail="would run 'uv cache prune' (removes unused wheels/sdists).",
        )
    run_env = {**os.environ, **dict(env.items()), "UV_CACHE_DIR": str(cache_dir)}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["uv", "cache", "prune"],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        trace.event("DISK-GC", "uv_cache_prune_failed label=%s err=%s", label, exc)
        return GCLocationResult(
            location=f"uv cache prune ({label})",
            path=str(cache_dir),
            reason="uv_prune_failed",
            bytes_freed=None,
            detail=f"uv cache prune failed: {exc}",
        )
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    freed = _parse_uv_freed(output)
    trace.event("DISK-GC", "uv_cache_prune label=%s rc=%d freed=%s", label, proc.returncode, freed)
    return GCLocationResult(
        location=f"uv cache prune ({label})",
        path=str(cache_dir),
        reason="uv_prune_ran" if proc.returncode == 0 else "uv_prune_nonzero_exit",
        bytes_freed=freed,
        detail=(output[-200:] or "(no output)"),
    )


def _temp_roots(env: Mapping[str, str]) -> list[Path]:
    """The temp roots to sweep for stale pytest basetemp trees (config/env → system temp)."""
    raw = (
        conf.resolve(
            "tools.mcp_cache.temp_roots",
            env="CLIO_MCP_CACHE_TEMP_ROOTS",
            default="",
            cast=conf.as_str,
        )
        or ""
    ).strip()
    roots: list[Path] = []
    if raw:
        roots.extend(Path(p.strip()).expanduser() for p in raw.split(os.pathsep) if p.strip())
    else:
        import tempfile  # noqa: PLC0415

        roots.append(Path(tempfile.gettempdir()))
    # de-dupe, keep order
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _prune_stale_temp(env: Mapping[str, str], *, now: float, dry_run: bool) -> GCLocationResult:
    """Remove pytest basetemp trees older than the configured age under the temp roots."""
    max_age_days = float(
        conf.resolve(
            "tools.mcp_cache.temp_max_age_days",
            env="CLIO_MCP_CACHE_TEMP_MAX_AGE_DAYS",
            default=_DEFAULT_TEMP_MAX_AGE_DAYS,
            cast=conf.as_float,
        )
    )
    cutoff = now - max_age_days * 86400.0
    freed = 0
    removed = 0
    scanned_roots: list[str] = []
    # The glob patterns overlap (``pytest-of-*`` also matches ``pytest-*``); dedupe by
    # resolved path so an overlapping match is never counted (or deleted) twice.
    seen: set[Path] = set()
    for root in _temp_roots(env):
        if not root.is_dir():
            continue
        scanned_roots.append(str(root))
        for pattern in _PYTEST_TEMP_GLOBS:
            for victim in root.glob(pattern):
                if victim in seen or not victim.is_dir():
                    continue
                seen.add(victim)
                try:
                    mtime = victim.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    continue
                size = _dir_size(victim)
                freed += size
                removed += 1
                if not dry_run:
                    shutil.rmtree(victim, ignore_errors=True)
                trace.event(
                    "DISK-GC",
                    "%sstale_temp_evicted path=%s freed=%d age_days=%.1f",
                    "would-" if dry_run else "",
                    victim,
                    size,
                    (now - mtime) / 86400.0,
                )
    return GCLocationResult(
        location="stale pytest basetemp",
        path=os.pathsep.join(scanned_roots),
        reason="age_over_max",
        bytes_freed=freed,
        removed=removed,
        detail=f"older than {max_age_days:g}d across {len(scanned_roots)} temp root(s).",
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run_gc(
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    peer_prober: Any = None,
) -> GCReport:
    """Run (or plan, under ``dry_run``) the deep disk reclamation — the #1001 fallback.

    REFUSES with a typed reason when any live clio peer is detected; otherwise prunes the
    four locations and returns a :class:`GCReport` with a per-location row.

    Args:
        dry_run: Compute the plan without deleting anything.
        env: Environment mapping (defaults to the process environment).
        now: Reference epoch seconds (injectable for tests).
        peer_prober: Zero-arg callable returning live peers (injected in tests); defaults
            to :func:`live_peer_clio_processes`.

    Returns:
        The :class:`GCReport`.
    """
    env = env if env is not None else os.environ
    now = time.time() if now is None else now
    prober = peer_prober if peer_prober is not None else live_peer_clio_processes

    peers = list(prober())
    if peers:
        trace.event(
            "DISK-GC",
            "gc_refused_live_peers count=%d (%s)",
            len(peers),
            ", ".join(str(p) for p in peers[:8]),
        )
        return GCReport(
            dry_run=dry_run,
            refused=True,
            refusal_reason="live_clio_peers_present",
            peers=peers,
        )

    report = GCReport(dry_run=dry_run)

    # 1. clio-owned MCP uv cache (configured bounds).
    from clio_agent.tools.mcp_config import _mcp_uv_cache_dir  # noqa: PLC0415

    bounds = mcp_cache.cache_bounds()
    prune = mcp_cache.prune_uv_cache(
        _mcp_uv_cache_dir(),
        max_bytes=bounds.max_bytes,
        max_age_days=bounds.max_age_days,
        now=now,
        dry_run=dry_run,
    )
    report.locations.append(
        GCLocationResult(
            location="mcp-uv-cache",
            path=prune.cache_dir,
            reason="over_budget_residual" if prune.over_budget_residual else "bounded",
            bytes_freed=prune.bytes_freed,
            removed=prune.count,
            detail=(
                f"before={prune.total_bytes_before} after={prune.total_bytes_after} "
                f"max_bytes={bounds.max_bytes}"
            ),
        )
    )

    # 2. clio-kit mcp-environments (newest-per-server).
    kit_cache = _clio_kit_cache_dir(env)
    report.locations.append(_prune_clio_kit_environments(kit_cache, dry_run=dry_run))

    # 3. uv cache prune (ambient + clio-kit private + mcp-uv-cache).
    ambient = _ambient_uv_cache_dir()
    if ambient is not None:
        report.locations.append(_uv_cache_prune("ambient", ambient, env=env, dry_run=dry_run))
    report.locations.append(
        _uv_cache_prune("mcp-uv-cache", Path(prune.cache_dir), env=env, dry_run=dry_run)
    )
    if kit_cache is not None:
        for sub in ("uv-cache", "cache", "uv"):
            candidate = kit_cache / sub
            if candidate.is_dir():
                report.locations.append(
                    _uv_cache_prune("clio-kit", candidate, env=env, dry_run=dry_run)
                )
                break

    # 4. stale pytest basetemp trees.
    report.locations.append(_prune_stale_temp(env, now=now, dry_run=dry_run))

    trace.event(
        "DISK-GC",
        "gc_complete dry_run=%s total_freed=%d locations=%d",
        dry_run,
        report.total_bytes_freed,
        len(report.locations),
    )
    return report


def _ambient_uv_cache_dir() -> Path | None:
    """Resolve the ambient uv cache dir (``uv cache dir``), tolerating uv absent."""
    if shutil.which("uv") is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["uv", "cache", "dir"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip()
    return Path(out) if out else None
