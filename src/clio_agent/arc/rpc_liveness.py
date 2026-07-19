"""Per-RPC liveness: failure-to-progress (stall) detection for clio-core calls (#948 S4).

Companion to :mod:`clio_agent.arc.clio_core_liveness` -- the socket-probe *gate* that
catches a DEAD daemon (connection refused) before a native op. This module catches the
case that gate cannot: a **zombie** daemon whose RPC port still ACCEPTS connections
while its runtime is internally dead (the WSAStartup-assert class), so a native op
CONNECTS successfully and then never returns -- freezing whatever thread issued it. On
the gact server that thread is the asyncio event loop, so a single stalled ARC RPC
takes down every SSE write, heartbeat, and HTTP handler at once (verified live).

What we detect is **failure to progress at the individual RPC level**, never an
absolute/wall-clock bound on an operation. Long-running work is legitimate and
UNBOUNDED -- a 12-hour agent run must never be killed for duration. A single ARC/CTE
call that produces no response for ``stall_after_s`` is a STALLED PEER, not a slow one.
The native binding exposes no partial-progress callback, so the whole-RPC no-response
IS the progress signal we watch -- stated honestly here, not worked around: there is
nothing finer to observe than "the call has not returned any bytes yet".

On a stall the call is retried ``retries`` times with a growing backoff
(``backoff_initial_s`` -> ``backoff_max_s``), RECONNECTING before each retry so a
zombie's already-accepted socket is never reused. Ladder exhausted -> the documented
typed degrade (:class:`~clio_agent.arc.clio_core_liveness.ClioCoreRuntimeLostError`,
reason :data:`RPC_STALLED_REASON`), which quarantines the store and reaches the trace
as a structured, attributable reason -- never a hang, never a silent fallback.

Knobs resolve config-first (config file -> env -> default, #985) through
:mod:`clio_agent.conf`; see :func:`resolve_liveness_policy`.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from clio_agent.arc.clio_core_liveness import ClioCoreRuntimeLostError

logger = logging.getLogger(__name__)

# Typed reasons (queryable in logs/trace), consistent with the #892 vocabulary.
RPC_STALLED_REASON = "clio_core_rpc_stalled"
RPC_RECONNECT_FAILED_REASON = "clio_core_rpc_reconnect_failed"

# Config-first defaults (#985). A single ARC/CTE RPC that returns nothing for
# ``stall_after_s`` is treated as a stalled peer; the ladder makes ``retries``
# further attempts with a backoff growing ``backoff_initial_s`` -> ``backoff_max_s``.
_DEFAULT_STALL_AFTER_S = 30.0
_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF_INITIAL_S = 2.0
_DEFAULT_BACKOFF_MAX_S = 15.0
# Backoff growth factor between successive stall retries.
_BACKOFF_FACTOR = 3.0


@dataclass(frozen=True)
class LivenessPolicy:
    """Resolved per-RPC stall policy (all times in seconds)."""

    stall_after_s: float
    retries: int
    backoff_initial_s: float
    backoff_max_s: float


def resolve_liveness_policy() -> LivenessPolicy:
    """Resolve the per-RPC stall policy config-first (file -> env -> default, #985).

    Reads ``arc.liveness.stall_after_s`` / ``arc.liveness.retries`` /
    ``arc.liveness.backoff_initial_s`` / ``arc.liveness.backoff_max_s`` (env overrides
    ``CLIO_ARC_LIVENESS_STALL_AFTER_S`` / ``_RETRIES`` / ``_BACKOFF_INITIAL_S`` /
    ``_BACKOFF_MAX_S``). Nonsensical values (a non-positive stall window, negative
    retries) fall back to the safe default rather than silently disabling the guard.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    stall = conf.resolve(
        "arc.liveness.stall_after_s",
        env="CLIO_ARC_LIVENESS_STALL_AFTER_S",
        default=_DEFAULT_STALL_AFTER_S,
        cast=conf.as_float,
    )
    retries = conf.resolve(
        "arc.liveness.retries",
        env="CLIO_ARC_LIVENESS_RETRIES",
        default=_DEFAULT_RETRIES,
        cast=conf.as_int,
    )
    backoff_initial = conf.resolve(
        "arc.liveness.backoff_initial_s",
        env="CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S",
        default=_DEFAULT_BACKOFF_INITIAL_S,
        cast=conf.as_float,
    )
    backoff_max = conf.resolve(
        "arc.liveness.backoff_max_s",
        env="CLIO_ARC_LIVENESS_BACKOFF_MAX_S",
        default=_DEFAULT_BACKOFF_MAX_S,
        cast=conf.as_float,
    )
    backoff_initial = backoff_initial if backoff_initial >= 0 else _DEFAULT_BACKOFF_INITIAL_S
    return LivenessPolicy(
        stall_after_s=stall if stall > 0 else _DEFAULT_STALL_AFTER_S,
        retries=retries if retries >= 0 else _DEFAULT_RETRIES,
        backoff_initial_s=backoff_initial,
        backoff_max_s=max(backoff_initial, backoff_max)
        if backoff_max > 0
        else _DEFAULT_BACKOFF_MAX_S,
    )


def _run_with_stall_watch(
    make_call: Callable[[], Any], stall_after_s: float
) -> tuple[bool, Any, Optional[BaseException]]:
    """Run ``make_call`` on a fresh daemon worker; watch for failure to progress.

    Returns ``(completed, value, error)``. ``completed`` is False iff the call did not
    return within ``stall_after_s`` (a stall). A completed call reports its value or the
    exception it raised (re-raised on the caller thread). A stalled worker is a daemon
    thread and is ABANDONED (a native hung RPC cannot be interrupted from Python); the
    quarantine-on-exhaustion below bounds the leak to the one op that detected the stall.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            box["value"] = make_call()
        except Exception as exc:  # noqa: BLE001 - captured, re-raised on the caller thread
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_runner, name="arc-rpc", daemon=True)
    worker.start()
    completed = done.wait(stall_after_s)
    return completed, box.get("value"), box.get("error")


def call_with_liveness(
    make_call: Callable[[], Any],
    *,
    op_name: str,
    port: int,
    reconnect: Callable[[], None],
    policy: Optional[LivenessPolicy] = None,
    on_exhausted: Optional[Callable[[str], None]] = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run one clio-core RPC with stall (failure-to-progress) detection + recovery.

    ``make_call`` MUST re-establish any per-op native handle it needs each time it is
    invoked (e.g. re-create the CTE ``Tag``), because it is re-run after a reconnect --
    a zombie's stale handle must never be reused. A stall triggers ``reconnect`` and a
    retry with growing backoff; the ladder exhausting raises the typed degrade (and
    calls ``on_exhausted(reason)`` first, so the store can quarantine).

    Args:
        make_call: Zero-arg thunk performing the native RPC (re-runnable).
        op_name: Short op label for logs/errors (``"get"`` / ``"put"`` / ...).
        port: The daemon RPC port (for the typed error + logs).
        reconnect: Re-establish the client/channel before a retry (raises on failure).
        policy: Resolved stall policy; ``None`` resolves the configured default.
        on_exhausted: Called with the reason when the ladder exhausts (before raising).
        _sleep: Injectable sleep (test seam).

    Raises:
        ClioCoreRuntimeLostError: The stall ladder exhausted (reason
            :data:`RPC_STALLED_REASON`) -- typed, trace-visible, never a hang.
        Exception: Any error the underlying RPC itself raised (propagated unmodified;
            a real RPC error is NOT a stall and is not retried here).
    """
    policy = policy or resolve_liveness_policy()
    backoff = policy.backoff_initial_s
    attempts = policy.retries + 1
    for attempt in range(attempts):
        completed, value, error = _run_with_stall_watch(make_call, policy.stall_after_s)
        if completed:
            if error is not None:
                raise error
            return value
        remaining = attempts - attempt - 1
        logger.warning(
            "clio-core RPC produced no response: reason=%s op=%s attempt=%d/%d "
            "stall_after_s=%s port=%s (peer appears to be a zombie); %s",
            RPC_STALLED_REASON,
            op_name,
            attempt + 1,
            attempts,
            policy.stall_after_s,
            port,
            "reconnecting and retrying" if remaining else "ladder exhausted -> typed degrade",
        )
        if not remaining:
            break
        _reconnect_before_retry(reconnect, op_name, port)
        _sleep(backoff)
        backoff = min(backoff * _BACKOFF_FACTOR, policy.backoff_max_s)
    if on_exhausted is not None:
        on_exhausted(RPC_STALLED_REASON)
    raise ClioCoreRuntimeLostError(
        f"clio-core RPC {op_name!r} produced no response within {policy.stall_after_s:g}s "
        f"across {attempts} attempt(s) on 127.0.0.1:{port}; the peer appears to be a zombie "
        "(accepting connections, runtime dead). The store is quarantined to avoid freezing "
        "the caller (the event loop).",
        reason=RPC_STALLED_REASON,
        port=port,
        details={"op": op_name, "attempts": attempts, "stall_after_s": policy.stall_after_s},
    )


def _reconnect_before_retry(reconnect: Callable[[], None], op_name: str, port: int) -> None:
    """Re-establish the client before a stall retry; a failure LADDERS on (never silent).

    A zombie's accepted socket must never be reused, so we reconnect first. A reconnect
    that itself raises is logged with a typed reason and the ladder continues to its
    bounded end (the next attempt re-tries) rather than swallowing the failure.
    """
    try:
        reconnect()
    except Exception as exc:  # noqa: BLE001 - laddered, not swallowed: logged with a typed reason
        logger.warning(
            "clio-core reconnect before stall-retry failed: reason=%s op=%s port=%s "
            "error=%s: %s (continuing the bounded ladder)",
            RPC_RECONNECT_FAILED_REASON,
            op_name,
            port,
            type(exc).__name__,
            exc,
        )


def guard_store_op(op_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a ``ClioCoreStore`` native-op method with the liveness gate + stall guard.

    The wrapped store must expose ``_live()`` (the #892 socket gate: a DEAD daemon
    raises typed before a worker thread is even spawned), ``_gate`` (a
    :class:`~clio_agent.arc.clio_core_liveness.LivenessGate` supplying ``port`` and
    ``note_rpc_stalled``), and ``_reconnect``. The whole method body is the re-runnable
    thunk, so a stall retry re-executes it against a freshly reconnected client.
    """

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            self._live()
            return call_with_liveness(
                lambda: method(self, *args, **kwargs),
                op_name=op_name,
                port=self._gate.port,
                reconnect=self._reconnect,
                on_exhausted=self._gate.note_rpc_stalled,
            )

        return wrapper

    return decorator
