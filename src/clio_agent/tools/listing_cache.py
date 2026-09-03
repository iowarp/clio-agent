"""Cached MCP tool listings: boot without spawning the fleet (#942).

The boot listing pass exists to learn STATIC metadata — a stdio server's tool
definitions change only when its launcher/env changes — yet it used to spawn
every declared chain concurrently to read them: the boot memory peak was the
sum of the whole fleet at the sampler's unluckiest tick (observed 1.27–1.50 GB
across identical-code runs; the v0.8.0 release gate blocked on it), and the
alternative (strictly serial live listing) costs tens of seconds on EVERY
boot.

This cache removes the trade-off:

- **Hit** (steady state): a namespace's definitions load from the cache —
  zero processes spawned, zero boot latency.
- **Miss/invalid**: the namespace lists LIVE — the caller serializes those and
  reaps each chain before the next spawns — and the result is stored.

Validity is strict reality-gating, the #934 discipline: entries are keyed by
(launcher, args, env-hash), invalidated by the launcher fingerprint (size:
mtime of ``command``) AND the args fingerprint (size:mtime of every LOCAL
FILE argument — #1308: for a ``python <script>``/``node <script>``-shaped
stdio launcher the SCRIPT argument, not the interpreter, defines the served
tools) changing, and by TTL (``CLIO_MCP_LISTING_TTL_H``, default 24h —
clio-kit servers resolve from remote registry state, so listings must expire
for upstream tool changes to reach users; for shim launchers neither the
binary nor a script argument typically moves, so the TTL is the ONLY
invalidation in the common case). Every stale/invalid entry is dropped with
a typed reason; a cache miss is not a degradation (the first boot always
lists live). Staleness while an entry lives: a tool ADDED upstream is
invisible (calls to it raise typed unknown-tool), a tool REMOVED upstream
stays listed (calls fail typed at the server) — both self-heal at expiry,
the same bounded trade as the #934 spawn diet.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from clio_agent import conf, paths
from clio_agent.runtime import trace

#: #1281 (C1-S1): bumped v1 -> v2 so a pre-fix cached listing (recorded before
#: capability discovery existed) cannot mask a task-capable server for up to
#: 24h -- the schema mismatch drops it, forcing a live re-list that also
#: records its task capability. Bumped v2 -> v3 (adversarial-review F3): a v2
#: entry stored NO capability fields at all, so BOTH cache-first callers
#: (``gateway.list_tool_definitions``, ``mcp_discovery._list_one_namespace``)
#: silently defeated the definitive read on every cache HIT for a whole TTL
#: window -- the entry now carries the negotiated verdict, and every hit
#: replays it through ``record_task_capability`` (see :func:`load_listing`).
#: Bumped v3 -> v4 (#1308): a v3 entry never fingerprinted its ARGS -- for a
#: ``python <script>``/``node <script>``-shaped stdio launcher (the common
#: shape for a declared MCP server), the SCRIPT that defines the served
#: tools is an arg, not ``command``, so editing it (e.g. adding an MCP Apps
#: ``ui`` declaration to a tool) never invalidated a v3 entry cached before
#: the edit -- a stale, meta-less tool definition silently served for up to
#: the TTL, which is exactly what made #1308's live Apps-host symptom
#: (a ui-bearing tool call succeeds with no ``mcp_app`` Part, zero trace)
#: undiagnosable. The bump drops every v3 entry outright (some may have
#: legitimately been fresh; they simply re-list live once, same cost as any
#: cold miss) and :func:`load_listing`/:func:`store_listing` now also check
#: :func:`_args_fingerprint` so a future script edit is caught going forward.
_SCHEMA = "clio-agent.mcp-listing-cache.v4"
_CACHE_BASENAME = "mcp_listing_cache.json"

_lock = threading.Lock()


def listing_ttl_h() -> float:
    """Listing-cache TTL in hours (#942 staleness bound)."""

    return float(
        conf.resolve(
            "tools.mcp.listing_ttl_h",
            env="CLIO_MCP_LISTING_TTL_H",
            default=24.0,
            cast=conf.as_float,
        )
    )


def _cache_path() -> Path:
    return paths.user_cache_dir() / _CACHE_BASENAME


def _launcher_fingerprint(command: str) -> str | None:
    """size:mtime of the resolved launcher binary; ``None`` when unresolvable."""

    resolved = shutil.which(command) or (command if Path(command).exists() else None)
    if resolved is None:
        return None
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    return f"{st.st_size}:{int(st.st_mtime)}"


def _args_fingerprint(args: tuple[str, ...]) -> str:
    """size:mtime of every LOCAL FILE argument, joined in order (#1308).

    ``_launcher_fingerprint`` alone only sees ``command`` -- the interpreter
    binary for a ``python <script>``/``node <script>``-shaped stdio launcher.
    For that common shape the SCRIPT argument, not the interpreter, defines
    the tools actually served (including their MCP Apps ``_meta.ui``
    declarations), so it must independently invalidate a cached entry when
    it changes. A non-file argument (a flag, a URL, an opaque token)
    contributes nothing -- only arguments that resolve to an existing local
    file are fingerprinted, so ordinary flags never spuriously invalidate.

    A DIFFERENT staleness class stays uncovered BY DESIGN (Opus review, C1-S4
    F2): a module/package-name launcher (``uvx <pkg>``, ``clio-kit
    mcp-server X``, ``python -m <module>``) names no local file argument at
    all, so this fingerprint sees nothing to invalidate on. An upstream
    package update is invisible here and bounded ONLY by
    :func:`listing_ttl_h`'s TTL -- the same bound already documented for
    clio-kit's remote-registry servers above.
    """

    parts: list[str] = []
    for arg in args:
        try:
            path = Path(arg)
            if not path.is_file():
                continue
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{arg}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


def entry_key(command: str, args: tuple[str, ...], env: Any = None) -> str:
    """Cache key: command + args + a HASH of the declared env.

    Two declarations with identical argv but different env (an API-key-gated
    toolset) must not share an entry, and editing a server's env must
    invalidate. The env is hashed, never stored — it may carry secrets.
    """

    import hashlib  # noqa: PLC0415

    env_items = sorted((str(k), str(v)) for k, v in (env or {}).items())
    env_hash = hashlib.sha256(json.dumps(env_items).encode()).hexdigest()[:16]
    return json.dumps([command, *args, env_hash])


def _load() -> dict[str, Any]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        trace.event("TOOLS", "mcp_listing_cache unreadable, ignoring: %s", exc)
        return {}
    if raw.get("schema") != _SCHEMA:
        trace.event(
            "TOOLS",
            "mcp_listing_cache schema %r != %r, dropping (listings refresh live)",
            raw.get("schema"),
            _SCHEMA,
        )
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save(entries: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": _SCHEMA, "entries": entries}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_listing(
    namespace: str, command: str, args: tuple[str, ...], env: Any = None
) -> list[Any] | None:
    """Return the cached MCPTool list for a spec, or ``None`` (list live).

    ``None`` is quiet on a plain miss; an INVALID entry (launcher changed,
    expired, malformed) is dropped with a typed reason before returning
    ``None`` so the live listing that follows refreshes it.

    #1281 F3 (adversarial review): a HIT that carries a persisted task
    capability (``task_capable``/``task_capability_source``, written by
    :func:`store_listing`) replays it through ``mcp_connection_era.
    record_task_capability`` -- keyed by ``namespace``, the same key the
    live definitive read uses -- so a warm namespace's capability is
    queryable without a live re-list forcing it. An entry with no persisted
    capability (pre-C1-S1, impossible post the v3 schema bump, but also a
    cacheable spec whose live listing never resolved a capability) replays
    nothing, leaving the registry exactly as a live miss would.
    """

    from mcp.types import Tool  # noqa: PLC0415 - deferred; hot path never imports it on a miss

    key = entry_key(command, args, env)
    entries = _load()
    entry = entries.get(key)
    if entry is None:
        return None

    def _drop(reason: str) -> None:
        trace.event("TOOLS", "mcp_listing_cache_invalid namespace=%s reason=%s", namespace, reason)
        try:
            with _lock:
                fresh = _load()
                if fresh.pop(key, None) is not None:
                    _save(fresh)
        except Exception as exc:  # noqa: BLE001 - a cache-file hiccup must never
            # cost the session its declared-tool surface (the caller's outer
            # except degrades the WHOLE catalog to built-ins).
            trace.event(
                "TOOLS", "mcp_listing_cache_drop_failed namespace=%s reason=%s", namespace, exc
            )

    fingerprint = _launcher_fingerprint(command)
    if fingerprint is None or entry.get("launcher_fingerprint") != fingerprint:
        _drop("launcher_changed")
        return None
    # #1308: the SCRIPT argument (not just the interpreter) defines what a
    # ``python <script>``-shaped launcher actually serves -- see
    # _args_fingerprint's docstring.
    if entry.get("args_fingerprint") != _args_fingerprint(args):
        _drop("args_changed")
        return None
    listed_at = entry.get("listed_at")
    if not isinstance(listed_at, (int, float)) or (
        time.time() - listed_at > listing_ttl_h() * 3600
    ):
        _drop("expired")
        return None
    raw_tools = entry.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        _drop("malformed")
        return None
    try:
        listed = [Tool.model_validate(t) for t in raw_tools]
    except Exception as exc:  # noqa: BLE001 - malformed entry is typed + dropped
        _drop(f"undecodable:{type(exc).__name__}")
        return None

    task_capable = entry.get("task_capable")
    source = entry.get("task_capability_source")
    era = entry.get("task_capability_era")
    if isinstance(task_capable, bool) and source in (
        "capabilities_extensions",
        "tool_execution",
        "none",
    ):
        from clio_agent.tools.mcp_connection_era import record_task_capability  # noqa: PLC0415

        record_task_capability(
            namespace,
            task_capable=task_capable,
            source=source,
            era=era if era in ("modern", "legacy", "unknown") else "unknown",
        )
    return listed


def store_listing(
    namespace: str,
    command: str,
    args: tuple[str, ...],
    tools: list[Any],
    env: Any = None,
    *,
    task_capable: bool | None = None,
    source: str | None = None,
    era: str | None = None,
) -> None:
    """Persist a live listing result. Never raises (boot must not fail on cache IO).

    ``task_capable``/``source``/``era`` (#1281 F3, adversarial review) are
    the negotiated verdict a caller resolved for THIS SAME live listing
    (e.g. via ``mcp_task_routing.capability_cache_fields(namespace)`` right
    after ``gateway._list_declared_tools`` recorded it) -- persisted
    alongside the tools so the NEXT cache hit can replay it
    (:func:`load_listing`) without needing a live re-connect. All-``None``
    (the default) persists no capability fields, matching pre-C1-S1 entries.
    """

    try:
        fingerprint = _launcher_fingerprint(command)
        if fingerprint is None or not tools:
            return  # nothing durable to anchor the entry on
        with _lock:
            entries = _load()
            entry: dict[str, Any] = {
                "namespace": namespace,
                "launcher_fingerprint": fingerprint,
                "args_fingerprint": _args_fingerprint(args),
                "listed_at": time.time(),
                # by_alias is LOAD-BEARING: Tool.meta is aliased "_meta" and
                # the model does not populate_by_name — without it the round
                # trip silently drops meta (and every MCP tag on it), forking
                # cached-boot catalog behavior from first-boot.
                "tools": [
                    t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools
                ],
            }
            if task_capable is not None:
                entry["task_capable"] = task_capable
                entry["task_capability_source"] = source
                entry["task_capability_era"] = era
            entries[entry_key(command, args, env)] = entry
            # Prune expired entries while we hold the file anyway — the cache
            # must not grow monotonically (test runs key by unique tmp paths).
            ttl_s = listing_ttl_h() * 3600
            now = time.time()
            entries = {
                k: v
                for k, v in entries.items()
                if isinstance(v.get("listed_at"), (int, float)) and now - v["listed_at"] <= ttl_s
            }
            _save(entries)
        trace.event("TOOLS", "mcp_listing_cached namespace=%s tools=%d", namespace, len(tools))
    except Exception as exc:  # noqa: BLE001 - cache IO failure must never fail boot
        trace.event(
            "TOOLS", "mcp_listing_cache_store_failed namespace=%s reason=%s", namespace, exc
        )
