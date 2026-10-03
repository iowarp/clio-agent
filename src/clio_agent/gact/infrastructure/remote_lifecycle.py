"""Launch ownership proof and graceful Desktop-exit cleanup for remote CLIO."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from clio_agent.gact.infrastructure.clio_agent_deploy import _COMMON, LAUNCHER_PRELUDE
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.infrastructure.store import InfrastructureStore

logger = logging.getLogger(__name__)


async def stop_desktop_launches(
    desktop_id: str,
    launches: dict[str, RemoteLaunch],
    store: InfrastructureStore,
    execute: Callable[[str, CommandSpec], Awaitable[CommandResult]],
    target_locks: dict[str, asyncio.Lock],
) -> list[str]:
    """Stop only this Desktop's nonpersistent launches and retain failed cleanup records."""

    failures: list[str] = []
    for target_id, launch in list(launches.items()):
        if launch.keep_running or launch.desktop_id != desktop_id:
            continue
        try:
            # Let an already-started operation finish recording its service
            # before stopping it; otherwise its late settle can mark it running.
            async with target_locks.setdefault(target_id, asyncio.Lock()):
                if launches.get(target_id) is not launch:
                    continue
                result = await execute(target_id, stop_owned_command(launch))
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr or result.stdout)
                launches.pop(target_id)
                store.update_service(target_id, "clio_agent", state="stopped")
            logger.info("desktop_remote_stopped target=%s", target_id)
        except (OSError, RuntimeError, KeyError) as exc:
            failures.append(f"{target_id}: {exc}")
            logger.warning("desktop_remote_stop_failed target=%s error=%s", target_id, exc)
    return failures


@dataclass(frozen=True)
class RemoteLaunch:
    """A unique launch belonging to this controller process, never an adopted service."""

    root: str
    port: int
    token: str
    keep_running: bool = False
    desktop_id: str = ""


def start_owned_command(launch: RemoteLaunch) -> CommandSpec:
    """Start a remote agent with a unique identifier inherited by its process."""

    script = (
        "# clio-deploy:start\n"
        + LAUNCHER_PRELUDE
        + _COMMON
        + r"""
export CLIO_PORT="$2" CLIO_DESKTOP_LAUNCH="$3"
# A second process may have taken the port since the initial claim. Never adopt
# it implicitly or claim its ownership just because the launcher says healthy.
port_busy "$port" && fail "Port $port became busy. Choose another port or reconnect."
"$bin/clio" start || exit $?
pid="$(cat "$(pidfile_for "$root")" 2>/dev/null || true)"
[ -n "$pid" ] && [ "$(stat -c %u "/proc/$pid" 2>/dev/null)" = "$(id -u)" ] || fail "Cannot verify the new CLIO process owner"
tr '\0' '\n' <"/proc/$pid/environ" | grep -Fxq "CLIO_DESKTOP_LAUNCH=$3" || fail "The running CLIO was not started by this deployment"
say "Started Desktop-owned CLIO (pid $pid, $root, port $port)"
"""
    )
    return CommandSpec(
        program="bash",
        args=["-lc", script, "clio", launch.root, str(launch.port), launch.token],
        timeout_seconds=120,
    )


def stop_owned_command(launch: RemoteLaunch) -> CommandSpec:
    """Stop only a same-account process carrying this exact launch identifier."""

    script = (
        "# clio-deploy:teardown\n"
        + _COMMON
        + r"""
pidfile="$(pidfile_for "$root")"
pid="$(cat "$pidfile" 2>/dev/null || true)"
if [ -z "$pid" ] || ! alive "$pid"; then exit 0; fi
[ "$(stat -c %u "/proc/$pid" 2>/dev/null)" = "$(id -u)" ] || fail "Leaving another user's process running"
owner="$(clio_prefix_of "$pid" || true)"
[ -n "$owner" ] && [ "$(real "$owner")" = "$(real "$root")" ] || fail "Leaving a replaced or shared process running"
tr '\0' '\n' <"/proc/$pid/environ" | grep -Fxq "CLIO_DESKTOP_LAUNCH=$3" || fail "Leaving a process from another launch running"
stop_clio_server "$pid" || fail "The Desktop-owned remote agent did not stop"
rm -f -- "$pidfile"
say "Stopped Desktop-owned CLIO (pid $pid); its installation and data remain"
"""
    )
    return CommandSpec(
        program="bash",
        args=["-lc", script, "clio", launch.root, str(launch.port), launch.token],
        timeout_seconds=45,
    )
