"""MCP spawn diet: learned direct venv-interpreter spawn for clio-kit servers (#930 S4/#934).

The declared launcher ``clio-kit mcp-server <name>`` spawns a 7-process chain on
Windows (trampoline -> bootstrap python -> clio-kit CLI -> resident ``uv run``
-> venv shim -> venv python -> server), ~90 MB of pure wrapper overhead per
namespace that stays resident for the life of the fleet. Only the leaf process
is the server.

This module collapses the chain from the SECOND spawn onward:

1. **Learn** — while the first (declared-command) chain is alive, walk our own
   process descendants to its leaf, capture the leaf argv plus the
   ``CLIO_KIT_*`` env the launcher injected, derive the direct spawn argv (the
   env's own interpreter shim next to the entry script, so ``pyvenv.cfg`` is
   honored), validate the layout invariant (the env dir's hash suffix is a
   prefix of ``CLIO_KIT_LOCKED_SERVER_PROJECT_SHA256``), and persist the plan.
2. **Apply** — ``transport_for`` consults :func:`resolve`; a validated plan
   replaces the declared argv. Validation is strict reality-gating: the plan is
   dropped (typed reason, declared command used, relearn re-armed) when the
   clio-kit launcher fingerprint changed, any recorded path vanished, the
   declared argv drifted, or the plan aged past its TTL. clio-kit therefore
   stays the authority on which env a server runs in — the diet never picks an
   env dir itself (multiple hash dirs per server are common and choosing one
   would silently run stale code).

**Staleness bound (the TTL).** clio-kit resolves each server's project from a
REMOTE registry at spawn time: new env dirs appear under an UNCHANGED launcher
binary, and old dirs are never deleted — so no local fingerprint can anchor
the env choice. A learned plan therefore expires after
``CLIO_MCP_SPAWN_DIET_TTL_H`` (default 24h): the next spawn takes the declared
chain (typed ``reason=plan_expired_relearn``), letting clio-kit re-resolve,
and the fresh chain is relearned. A dieted spawn that fails to CONNECT drops
its plan the same way (:func:`spawn_failed`) — one bad plan can never brick a
server persistently. The durable fix is an upstream clio-kit resolve probe
(print the project sha without spawning); until then the TTL bounds staleness.

Every outcome is typed: ``mcp_spawn_diet applied|learned`` and
``mcp_spawn_diet_fallback reason=...`` trace events. A cache miss is not a
degradation (the first spawn always takes the declared chain — that chain is
what we learn from); a VALIDATION failure is, and says why. The plan cache is
keyed by (resolved command, args) only — clio-kit's env choice is
cwd-independent (its cache is global per-user), so workspace identity never
enters the plan.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent import conf, paths
from clio_agent.runtime import trace

_SCHEMA = "clio-agent.mcp-spawn-diet.v1"
_CACHE_BASENAME = "mcp_spawn_diet.json"

# The recognized launcher and subcommand — the diet applies ONLY to the exact
# vanilla form ``clio-kit mcp-server <name>``. Extra args (e.g. ``--branch``)
# change which env clio-kit materializes, so those spawns are never dieted.
_LAUNCHER_BASENAMES = {"clio-kit", "clio-kit.exe"}
_SUBCOMMAND = "mcp-server"

# Env keys the launcher injects that the leaf server reads (lock verification).
_REPLAY_ENV_PREFIX = "CLIO_KIT_LOCKED_SERVER_"

# Learn scan schedule (seconds after the namespace CONNECTED — the chain is
# alive then; it only needs a moment to reach its leaf. A cold ``uv run``
# materialization takes longer, so the scan retries before giving up, typed).
_LEARN_DELAYS_S = (3.0, 10.0, 30.0, 60.0)

_scan_lock = threading.Lock()
_scans_scheduled: set[str] = set()
# Namespaces whose next live chain should be learned: registered at transport
# build (pre-spawn), fired by the executor at actual namespace connect —
# scheduling scans at build time misses lazy (#932) spawns entirely.
_pending_learns: dict[str, tuple[str, str, tuple[str, ...]]] = {}
# namespace -> plan key of the plan applied to its LAST spawn; consumed by
# spawn_failed to drop a plan whose spawn could not connect. Keyed by
# namespace only while executors are per-workspace: concurrent executors for
# the same namespace share one record, so an interleaved apply/drop can miss
# one learn window or no-op one drop — both self-heal (same plan key, next
# spawn re-applies/relearns), accepted over per-executor plumbing.
_applied_plans: dict[str, str] = {}


def spawn_diet_enabled() -> bool:
    """Operational kill switch for the learned direct spawn (#934).

    Default ON. Turning it off is a typed degradation (every eligible spawn
    logs ``reason=disabled_by_config``), never a silent alternate path.
    """

    return bool(
        conf.resolve(
            "tools.mcp.spawn_diet",
            env="CLIO_MCP_SPAWN_DIET",
            default=True,
            cast=conf.as_bool,
        )
    )


def spawn_diet_ttl_h() -> float:
    """Plan TTL in hours (#934 staleness bound — see the module docstring)."""

    return float(
        conf.resolve(
            "tools.mcp.spawn_diet_ttl_h",
            env="CLIO_MCP_SPAWN_DIET_TTL_H",
            default=24.0,
            cast=conf.as_float,
        )
    )


def _cache_path() -> Path:
    return paths.user_cache_dir() / _CACHE_BASENAME


def _launcher_fingerprint(resolved_command: str) -> str:
    """Cheap invalidation key for the launcher binary (upgrade => relearn)."""

    st = os.stat(resolved_command)
    return f"{st.st_size}:{int(st.st_mtime)}"


def _plan_key(spec_command: str, spec_args: tuple[str, ...]) -> str:
    return json.dumps([spec_command, *spec_args])


def diet_eligible(command: str, args: tuple[str, ...] | list[str]) -> bool:
    """Whether a declared stdio command is the exact vanilla clio-kit form."""

    base = Path(command).name.lower()
    if base not in _LAUNCHER_BASENAMES:
        return False
    args = tuple(args)
    return len(args) == 2 and args[0] == _SUBCOMMAND and not args[1].startswith("-")


def _load_cache() -> dict[str, Any]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        trace.event("TOOLS", "mcp_spawn_diet cache unreadable, ignoring: %s", exc)
        return {}
    if raw.get("schema") != _SCHEMA:
        trace.event(
            "TOOLS",
            "mcp_spawn_diet cache schema %r != %r, dropping (plans relearn)",
            raw.get("schema"),
            _SCHEMA,
        )
        return {}
    plans = raw.get("plans")
    return plans if isinstance(plans, dict) else {}


def _save_cache(plans: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": _SCHEMA, "plans": plans}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def resolve(
    spec_name: str, resolved_command: str, spec_args: tuple[str, ...]
) -> dict[str, Any] | None:
    """Return a validated spawn plan ``{argv, env}`` or ``None`` (typed reason).

    ``None`` with no trace when there is simply no plan yet (the first spawn
    is the learning spawn — not a degradation). ``None`` WITH a typed
    ``mcp_spawn_diet_fallback`` when a recorded plan failed validation.
    """

    key = _plan_key(resolved_command, spec_args)
    plans = _load_cache()
    plan = plans.get(key)
    if plan is None:
        return None

    def _drop(reason: str) -> None:
        """Typed fallback that also re-arms the relearn loop: the invalid plan
        leaves the cache and its scan-dedup key is released, so the declared
        spawn now running gets learned fresh."""

        trace.event("TOOLS", "mcp_spawn_diet_fallback server=%s reason=%s", spec_name, reason)
        with _scan_lock:
            fresh = _load_cache()
            if fresh.pop(key, None) is not None:
                _save_cache(fresh)
            _scans_scheduled.discard(key)

    try:
        fingerprint = _launcher_fingerprint(resolved_command)
    except OSError:
        _drop("launcher_unstatable")
        return None
    if plan.get("launcher_fingerprint") != fingerprint:
        _drop("launcher_changed")
        return None
    learned_at = plan.get("learned_at")
    ttl_s = spawn_diet_ttl_h() * 3600
    if not isinstance(learned_at, (int, float)) or time.time() - learned_at > ttl_s:
        # clio-kit resolves envs from remote registry state — a local plan
        # must expire so upstream server updates ever reach the user.
        _drop("plan_expired_relearn")
        return None
    argv = plan.get("argv")
    if not isinstance(argv, list) or len(argv) < 2:
        _drop("malformed_plan")
        return None
    if not all(Path(part).exists() for part in argv[:2]):
        _drop("env_vanished")
        return None
    env = plan.get("env")
    if not isinstance(env, dict) or not all(
        str(k).startswith(_REPLAY_ENV_PREFIX) for k in env
    ):
        # Enforced at APPLY, not just capture: a plan env key outside the
        # replay prefix could clobber workspace pinning (CLIO_KIT_ARTIFACTS).
        _drop("malformed_plan")
        return None
    return {"argv": [str(a) for a in argv], "env": {str(k): str(v) for k, v in env.items()}}


def _walk_to_leaf(root: "Any") -> "Any":
    """Deepest non-conhost descendant (largest-RSS child at each level)."""

    import psutil

    cur = root
    while True:
        try:
            kids = [k for k in cur.children() if k.name().lower() != "conhost.exe"]
        except psutil.Error:
            return cur
        if not kids:
            return cur
        cur = max(kids, key=lambda k: _rss_or_zero(k))


def _rss_or_zero(proc: "Any") -> int:
    import psutil

    try:
        return int(proc.memory_info().rss)
    except psutil.Error:
        return 0


def _derive_diet_argv(leaf_argv: list[str]) -> list[str] | None:
    """Swap the base interpreter for the env's own shim next to the entry script.

    The observed leaf runs ``<base-python> <env>/Scripts/<name>-mcp.exe`` —
    respawning that argv directly fails (base python never reads the env's
    ``pyvenv.cfg``). The env's interpreter shim beside the entry script does.
    """

    if len(leaf_argv) < 2:
        return None
    entry = Path(leaf_argv[1])
    shim = entry.parent / ("python.exe" if sys.platform == "win32" else "python")
    if not shim.exists() or not entry.exists():
        return None
    return [str(shim), *leaf_argv[1:]]


def _chain_argv_matches(cmdline: list[str], wanted_base: str, spec_args: tuple[str, ...]) -> bool:
    """EXACT match of a candidate chain root against the declared argv.

    A prefix match would learn from a VARIANT invocation (e.g. ``--branch
    dev``) whose env differs — the plan would then silently run that variant
    for vanilla spawns. On POSIX a uv-tool launcher is a shebang script, so
    the root's argv is ``[<python>, <launcher>, *args]`` — both shapes match.
    """

    direct = [Path(cmdline[0]).name.lower(), *cmdline[1:]]
    if direct == [wanted_base, *spec_args]:
        return True
    if len(cmdline) >= 2 and Path(cmdline[0]).name.lower().startswith("python"):
        shebang = [Path(cmdline[1]).name.lower(), *cmdline[2:]]
        return shebang == [wanted_base, *spec_args]
    return False


def _learn_scan(spec_name: str, resolved_command: str, spec_args: tuple[str, ...]) -> str:
    """One scan pass over our descendants.

    Returns a status: ``"learned"`` on success, else why not —
    ``"chain_not_found"`` (no process matches the declared argv),
    ``"leaf_not_ready"`` (chain exists, leaf not reached / churned),
    ``"underivable_leaf"`` / ``"layout_mismatch"`` (deepest process failed a
    learn guard — RETRYABLE: mid-materialization the deepest process is an
    intermediate wrapper that fails the same guards a foreign leaf would).
    The caller emits ONE typed give-up with the last status; scan passes
    themselves stay quiet.
    """

    import psutil

    wanted_base = Path(resolved_command).name.lower()
    me = psutil.Process()
    for child in me.children(recursive=True):
        try:
            cmdline = child.cmdline()
        except psutil.Error:
            continue
        if not cmdline or not _chain_argv_matches(cmdline, wanted_base, spec_args):
            continue
        leaf = _walk_to_leaf(child)
        if leaf.pid == child.pid:
            return "leaf_not_ready"  # chain not materialized yet — caller retries
        try:
            leaf_argv = leaf.cmdline()
            leaf_env = leaf.environ()
        except psutil.Error:
            return "leaf_not_ready"  # leaf churned mid-walk — caller retries
        diet_argv = _derive_diet_argv(leaf_argv)
        if diet_argv is None:
            return "underivable_leaf"
        replay_env = {
            k: v for k, v in leaf_env.items() if k.startswith(_REPLAY_ENV_PREFIX)
        }
        # Learn guards — refuse anything this schema does not fully recognize:
        # the env dir must be THIS server's (``<name>-<hash>``), and its hash
        # must prefix the project sha the launcher stamped into the leaf env.
        # The largest-RSS walk could land on a subprocess the server itself
        # spawned; these two anchors reject anything but a locked-server leaf.
        sha = replay_env.get(f"{_REPLAY_ENV_PREFIX}PROJECT_SHA256", "")
        env_dir = Path(diet_argv[1]).parent.parent.name
        suffix = env_dir.rsplit("-", 1)[-1] if "-" in env_dir else ""
        server = spec_args[1] if len(spec_args) > 1 else spec_name
        if not (
            sha
            and suffix
            and sha.startswith(suffix)
            and env_dir.startswith(f"{server}-")
        ):
            return "layout_mismatch"
        try:
            fingerprint = _launcher_fingerprint(resolved_command)
        except OSError:
            return "launcher_unstatable"
        with _scan_lock:
            plans = _load_cache()
            plans[_plan_key(resolved_command, spec_args)] = {
                "server": spec_name,
                "argv": diet_argv,
                "env": replay_env,
                "launcher_fingerprint": fingerprint,
                "learned_at": time.time(),
            }
            _save_cache(plans)
        trace.event(
            "TOOLS",
            "mcp_spawn_diet learned server=%s argv0=%s",
            spec_name,
            diet_argv[0],
        )
        return "learned"
    return "chain_not_found"


def register_pending_learn(
    spec_name: str, resolved_command: str, spec_args: tuple[str, ...]
) -> None:
    """Mark a namespace as learn-on-connect.

    Called by ``transport_for`` at transport BUILD time — under lazy routing
    (#932) that is minutes before any chain exists, so the scan itself must
    wait for :func:`namespace_connected`.
    """

    with _scan_lock:
        _pending_learns[spec_name] = (spec_name, resolved_command, spec_args)


def namespace_connected(namespace: str) -> None:
    """Executor hook: a namespace's stdio chain just connected — learn it.

    No-op unless the namespace registered as learn-on-connect. Never raises
    (the caller is the live tool-routing path).
    """

    try:
        with _scan_lock:
            if _applied_plans.pop(namespace, None) is not None:
                # This connect was a DIETED spawn succeeding — there is no
                # declared chain to learn from (and nothing left to drop).
                return
            pending = _pending_learns.get(namespace)
        if pending is None:
            return
        schedule_learn(*pending)
    except Exception as exc:  # noqa: BLE001 - never break tool routing
        trace.event("TOOLS", "mcp_spawn_diet learn_failed server=%s reason=%s", namespace, exc)


def schedule_learn(spec_name: str, resolved_command: str, spec_args: tuple[str, ...]) -> None:
    """Schedule a one-shot background learn against a LIVE chain.

    Fired by :func:`namespace_connected` when the chain exists. Retries on a
    short schedule (cold materialization is slow), then gives up with a typed
    reason. Deduped per plan key while a scan is in flight or succeeded.
    """

    key = _plan_key(resolved_command, spec_args)
    with _scan_lock:
        if key in _scans_scheduled:
            return
        _scans_scheduled.add(key)

    def _run() -> None:
        try:
            elapsed = 0.0
            status = "chain_not_found"
            for delay in _LEARN_DELAYS_S:
                threading.Event().wait(delay - elapsed)
                elapsed = delay
                status = _learn_scan(spec_name, resolved_command, spec_args)
                if status == "learned":
                    break
            if status != "learned":
                trace.event(
                    "TOOLS",
                    "mcp_spawn_diet learn_gave_up server=%s reason=%s "
                    "(retries on a future connect)",
                    spec_name,
                    status,
                )
            with _scan_lock:
                if status != "learned":
                    _scans_scheduled.discard(key)  # a future connect may retry
        except Exception as exc:  # noqa: BLE001 - the learner must never kill its host
            trace.event("TOOLS", "mcp_spawn_diet learn_failed server=%s reason=%s", spec_name, exc)
            with _scan_lock:
                _scans_scheduled.discard(key)

    threading.Thread(target=_run, name=f"clio-mcp-spawn-diet-{spec_name}", daemon=True).start()


def diet_transport_args(
    spec_name: str,
    resolved_command: str,
    spec_args: tuple[str, ...],
    env: Mapping[str, str],
) -> tuple[str, list[str], dict[str, str]] | None:
    """The transport_for seam: a validated (command, args, env) or ``None``.

    ``None`` means: spawn the declared command (and, when the spec is
    eligible, a learn scan has been scheduled against that live chain).
    """

    if not diet_eligible(resolved_command, spec_args):
        return None
    if "_" in spec_name:
        # #932 routing partitions tool names on the FIRST underscore, so an
        # underscore-named mount routes via the composite where the first-call
        # hooks (learn / drop-plan feedback) never fire. Refuse the diet for
        # such specs rather than run it without its feedback loop.
        trace.event(
            "TOOLS",
            "mcp_spawn_diet_fallback server=%s reason=underscore_mount_unroutable",
            spec_name,
        )
        return None
    if not spawn_diet_enabled():
        trace.event(
            "TOOLS", "mcp_spawn_diet_fallback server=%s reason=disabled_by_config", spec_name
        )
        return None
    plan = resolve(spec_name, resolved_command, spec_args)
    if plan is None:
        if shutil.which(resolved_command) or Path(resolved_command).exists():
            # The declared chain is about to spawn LAZILY (#932) — register
            # so the executor's connect hook fires the learn scan then.
            register_pending_learn(spec_name, resolved_command, spec_args)
        return None
    merged_env = {**env, **plan["env"]}
    argv = plan["argv"]
    with _scan_lock:
        _applied_plans[spec_name] = _plan_key(resolved_command, spec_args)
        # Keep the pending registration: if this dieted spawn fails to
        # connect, spawn_failed drops the plan and the declared respawn
        # relearns through the same connect hook.
        _pending_learns[spec_name] = (spec_name, resolved_command, spec_args)
    trace.event("TOOLS", "mcp_spawn_diet applied server=%s argv0=%s", spec_name, argv[0])
    return argv[0], argv[1:], merged_env


def spawn_failed(namespace: str) -> None:
    """Executor hook: a namespace's stdio connect failed — drop its dieted plan.

    No-op when the namespace's last spawn was not dieted. A plan whose spawn
    cannot connect must never be applied again (typed): the next spawn takes
    the declared chain and the connect hook relearns from it.
    """

    try:
        with _scan_lock:
            key = _applied_plans.pop(namespace, None)
            if key is None:
                return
            plans = _load_cache()
            if plans.pop(key, None) is not None:
                _save_cache(plans)
            _scans_scheduled.discard(key)
        trace.event(
            "TOOLS",
            "mcp_spawn_diet plan_dropped server=%s reason=spawn_failed",
            namespace,
        )
    except Exception as exc:  # noqa: BLE001 - never mask the original connect error
        trace.event("TOOLS", "mcp_spawn_diet plan_drop_failed server=%s reason=%s", namespace, exc)
