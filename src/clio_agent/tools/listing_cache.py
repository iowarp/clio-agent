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
(launcher, args, env-hash), invalidated by launcher fingerprint (size:mtime)
change and by TTL (``CLIO_MCP_LISTING_TTL_H``, default 24h — clio-kit servers
resolve from remote registry state, so listings must expire for upstream tool
changes to reach users; for shim launchers the binary essentially never moves,
so the TTL is the ONLY invalidation in the common case). Every stale/invalid
entry is dropped with a typed reason; a cache miss is not a degradation (the
first boot always lists live). Staleness while an entry lives: a tool ADDED
upstream is invisible (calls to it raise typed unknown-tool), a tool REMOVED
upstream stays listed (calls fail typed at the server) — both self-heal at
expiry, the same bounded trade as the #934 spawn diet.
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
#: records its task capability.
_SCHEMA = "clio-agent.mcp-listing-cache.v2"
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
        return [Tool.model_validate(t) for t in raw_tools]
    except Exception as exc:  # noqa: BLE001 - malformed entry is typed + dropped
        _drop(f"undecodable:{type(exc).__name__}")
        return None


def store_listing(
    namespace: str, command: str, args: tuple[str, ...], tools: list[Any], env: Any = None
) -> None:
    """Persist a live listing result. Never raises (boot must not fail on cache IO)."""

    try:
        fingerprint = _launcher_fingerprint(command)
        if fingerprint is None or not tools:
            return  # nothing durable to anchor the entry on
        with _lock:
            entries = _load()
            entries[entry_key(command, args, env)] = {
                "namespace": namespace,
                "launcher_fingerprint": fingerprint,
                "listed_at": time.time(),
                # by_alias is LOAD-BEARING: Tool.meta is aliased "_meta" and
                # the model does not populate_by_name — without it the round
                # trip silently drops meta (and every MCP tag on it), forking
                # cached-boot catalog behavior from first-boot.
                "tools": [
                    t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools
                ],
            }
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
