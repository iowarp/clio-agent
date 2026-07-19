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
from concurrent.futures import Future
from concurrent.futures import wait as _futures_wait
from dataclasses import dataclass
from typing import Any, Callable, Optional

from clio_agent.arc.clio_core_liveness import RPC_STALLED_REASON, ClioCoreRuntimeLostError

logger = logging.getLogger(__name__)

# Typed reasons (queryable in logs/trace), consistent with the #892 vocabulary.
# ``RPC_STALLED_REASON`` is owned by :mod:`clio_agent.arc.clio_core_liveness` (the
# quarantine-reason owner) and re-exported here for callers/tests that import it from
# this module; a single source of truth keeps the gate's reason-aware recovery and the
# stall degrade in lock-step.
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

# Upper bound on the SINGLE-attempt RPC-level health probe the gate runs to leave an
# ``rpc_stalled`` quarantine (:func:`probe_rpc_health`). The probe window is
# ``min(stall_after_s, _HEALTH_PROBE_MAX_S)`` so a re-probe of a still-hung zombie costs
# at most this, never the full stall window, and never the retry ladder.
_HEALTH_PROBE_MAX_S = 10.0


def _resolve_guarded(resolve: Callable[[], Any], *, env: str, default: Any) -> Any:
    """Run a ``conf.resolve`` thunk, falling back to ``default`` on a PARSE error.

    Mirrors :func:`clio_agent.arc.clio_core_liveness._resolve_liveness_ttl_s`: a
    malformed file/env value (``arc.liveness.stall_after_s: "thirty"``) must emit a
    structured warning and fall back to the safe default, never raise an untyped
    ``ValueError`` on the per-op ARC path. Semantic clamps (non-positive stall,
    negative retries) are applied by the caller after this returns. The literal
    ``conf.resolve(...)`` stays inside the thunk so the env-reference generator still
    AST-discovers the four knobs.
    """
    try:
        return resolve()
    except (ValueError, TypeError) as exc:
        logger.warning(
            "clio-core liveness: ignoring malformed env %s: %s: %s; using default %r",
            env,
            type(exc).__name__,
            exc,
            default,
        )
        return default


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
    ``_BACKOFF_MAX_S``). Two failure classes both fall back to the safe default rather
    than disabling the guard: a PARSE error (``"thirty"``) is caught per-knob by
    :func:`_resolve_guarded` (structured warning, no untyped ``ValueError`` on the op
    path), and a semantically-nonsensical value (non-positive stall, negative retries)
    is clamped below.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    stall = _resolve_guarded(
        lambda: conf.resolve(
            "arc.liveness.stall_after_s",
            env="CLIO_ARC_LIVENESS_STALL_AFTER_S",
            default=_DEFAULT_STALL_AFTER_S,
            cast=conf.as_float,
        ),
        env="CLIO_ARC_LIVENESS_STALL_AFTER_S",
        default=_DEFAULT_STALL_AFTER_S,
    )
    retries = _resolve_guarded(
        lambda: conf.resolve(
            "arc.liveness.retries",
            env="CLIO_ARC_LIVENESS_RETRIES",
            default=_DEFAULT_RETRIES,
            cast=conf.as_int,
        ),
        env="CLIO_ARC_LIVENESS_RETRIES",
        default=_DEFAULT_RETRIES,
    )
    backoff_initial = _resolve_guarded(
        lambda: conf.resolve(
            "arc.liveness.backoff_initial_s",
            env="CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S",
            default=_DEFAULT_BACKOFF_INITIAL_S,
            cast=conf.as_float,
        ),
        env="CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S",
        default=_DEFAULT_BACKOFF_INITIAL_S,
    )
    backoff_max = _resolve_guarded(
        lambda: conf.resolve(
            "arc.liveness.backoff_max_s",
            env="CLIO_ARC_LIVENESS_BACKOFF_MAX_S",
            default=_DEFAULT_BACKOFF_MAX_S,
            cast=conf.as_float,
        ),
        env="CLIO_ARC_LIVENESS_BACKOFF_MAX_S",
        default=_DEFAULT_BACKOFF_MAX_S,
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


# --------------------------------------------------------------------------- #
# shared bounded DAEMON worker pool (the stall-watch executor)
# --------------------------------------------------------------------------- #
#
# Every guarded RPC runs on a worker OFF the caller thread so a hung native call
# freezes a worker, never the event loop. The workers are pooled (module-level, lazily
# grown on demand up to a hard bound) so the common HEALTHY case never pays a per-op
# thread create/start/join — only a queue hand-off to an idle worker.
#
# The workers MUST be daemon threads: a worker abandoned on a hung native RPC (a zombie
# that never returns) must never block interpreter shutdown. ``concurrent.futures``
# ThreadPoolExecutor uses NON-daemon workers and joins them all at exit, so a single
# stalled worker would wedge the gact server's atexit runtime-release — exactly the
# freeze this module exists to prevent. Hence a small purpose-built daemon pool rather
# than ThreadPoolExecutor.
#
# ABANDONED-ON-STALL: a stalled worker is left running its native call (Python cannot
# interrupt it) and keeps occupying its slot; the pool grows a replacement for the next
# op up to ``_STALL_WATCH_MAX_WORKERS``. That bound IS the leak cap: at most this many
# hung-daemon threads can accumulate before further guarded ops queue and time out as
# stalls themselves (the store is quarantined long before, so this is a backstop).
_STALL_WATCH_MAX_WORKERS = 8

_pool_lock = threading.Lock()
_pool_threads_started = 0  # total workers ever spawned (<= _STALL_WATCH_MAX_WORKERS)
_pool_idle = 0  # workers currently blocked waiting for the next item
_work_queue: "_WorkQueue" = None  # type: ignore[assignment]  # lazily created under the lock


class _WorkQueue:
    """A minimal blocking hand-off queue (avoids importing ``queue`` for one use)."""

    def __init__(self) -> None:
        self._items: list[tuple[Callable[[], Any], Future]] = []
        self._cv = threading.Condition()

    def put(self, item: tuple[Callable[[], Any], Future]) -> None:
        with self._cv:
            self._items.append(item)
            self._cv.notify()

    def get(self) -> tuple[Callable[[], Any], Future]:
        with self._cv:
            while not self._items:
                self._cv.wait()
            return self._items.pop(0)


def _stall_watch_worker() -> None:
    """Daemon worker loop: pull a ``(thunk, future)`` and fulfil the future.

    A thunk that never returns (a hung native RPC) blocks this worker forever — it is
    abandoned by design; the future's caller has already stopped waiting.
    """
    global _pool_idle
    while True:
        with _pool_lock:
            _pool_idle += 1
        fn, fut = _work_queue.get()
        with _pool_lock:
            _pool_idle -= 1
        if not fut.set_running_or_notify_cancel():  # pragma: no cover - never cancelled
            continue
        try:
            fut.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller via the future
            fut.set_exception(exc)


def _submit_stall_watch(make_call: Callable[[], Any]) -> Future:
    """Hand ``make_call`` to an idle pooled worker, growing the pool up to its bound."""
    global _pool_threads_started, _work_queue
    fut: Future = Future()
    with _pool_lock:
        if _work_queue is None:
            _work_queue = _WorkQueue()
        # Grow only when no worker is idle to take the item and we are under the bound.
        if _pool_idle == 0 and _pool_threads_started < _STALL_WATCH_MAX_WORKERS:
            _pool_threads_started += 1
            threading.Thread(target=_stall_watch_worker, name="arc-rpc", daemon=True).start()
    _work_queue.put((make_call, fut))
    return fut


def _run_with_stall_watch(
    make_call: Callable[[], Any], stall_after_s: float
) -> tuple[bool, Any, Optional[BaseException]]:
    """Run ``make_call`` on a pooled daemon worker; watch for failure to progress.

    Returns ``(completed, value, error)``. ``completed`` is False iff the call did not
    return within ``stall_after_s`` (a stall). A completed call reports its value or the
    exception it raised (re-raised on the caller thread). A stalled worker is a daemon
    thread and is ABANDONED (a native hung RPC cannot be interrupted from Python); the
    pool bound (:data:`_STALL_WATCH_MAX_WORKERS`) caps the resulting leak.
    """
    fut = _submit_stall_watch(make_call)
    done, _ = _futures_wait([fut], timeout=stall_after_s)
    if fut not in done:
        return False, None, None  # stalled: worker abandoned, still holds its pool slot
    error = fut.exception()
    if error is not None:
        return True, None, error
    return True, fut.result(), None


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


def probe_rpc_health(
    *,
    reconnect: Callable[[], None],
    make_probe_call: Callable[[], Any],
    port: int,
    policy: Optional[LivenessPolicy] = None,
) -> bool:
    """One SINGLE-attempt RPC-LEVEL health probe used to leave an ``rpc_stalled`` quarantine.

    A zombie daemon defeats the socket probe (its port still ACCEPTS), so recovering
    from an ``rpc_stalled`` quarantine must confirm the daemon answers a REAL RPC, not
    just a TCP connect. This reconnects (a zombie's stale handle is never reused) and
    runs one cheap RPC (``make_probe_call`` — e.g. ``GetBlobSize`` on a sentinel key)
    through a single short stall watch (``min(stall_after_s, _HEALTH_PROBE_MAX_S)``, NO
    retries, NO ladder). It NEVER raises: a reconnect failure, a stall, or any RPC error
    all return ``False`` (stay quarantined). Only a clean return within the window is
    treated as recovered (``True``).

    Args:
        reconnect: Re-establish the client/channel before the probe (a zombie's socket
            is already accepted; this refreshes the handle).
        make_probe_call: Zero-arg thunk performing ONE cheap real RPC (re-runnable).
        port: The daemon RPC port (logs only).
        policy: Resolved stall policy; ``None`` resolves the configured default.

    Returns:
        ``True`` iff the probe RPC returned cleanly within the window; ``False`` otherwise.
    """
    policy = policy or resolve_liveness_policy()
    window = min(policy.stall_after_s, _HEALTH_PROBE_MAX_S)
    try:
        reconnect()
    except Exception as exc:  # noqa: BLE001 - a reconnect that fails is NOT recovered
        logger.warning(
            "clio-core RPC health probe: reconnect failed: reason=%s port=%s error=%s: %s "
            "(store stays quarantined)",
            RPC_RECONNECT_FAILED_REASON,
            port,
            type(exc).__name__,
            exc,
        )
        return False
    completed, _value, error = _run_with_stall_watch(make_probe_call, window)
    if not completed:
        logger.warning(
            "clio-core RPC health probe: no response within %gs (reason=%s port=%s); the peer "
            "still appears to be a zombie — store stays quarantined",
            window,
            RPC_STALLED_REASON,
            port,
        )
        return False
    if error is not None:
        logger.warning(
            "clio-core RPC health probe: RPC errored (port=%s error=%s: %s); store stays "
            "quarantined until a clean probe",
            port,
            type(error).__name__,
            error,
        )
        return False
    return True


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


def guarded_store_rpc(store: Any, op_name: str, make_call: Callable[..., Any], *args: Any) -> Any:
    """Run ONE native store RPC through the socket gate + per-RPC stall watch.

    The RPC-granularity companion to :func:`guard_store_op`: a store's MULTI-RPC methods
    (``put`` / ``clear``) call this per native call so ``stall_after_s`` bounds a single
    RPC, never the whole method — a legitimately long, progressing multi-blob clear (each
    ``DelBlob`` prompt, total > the window) is never misclassified as a stalled peer.
    ``*args`` are forwarded to ``make_call`` each call so a loop variable is passed as an
    argument (not captured), keeping the thunk re-runnable across a stall retry.
    """
    store._live()
    return call_with_liveness(
        lambda: make_call(*args),
        op_name=op_name,
        port=store._gate.port,
        reconnect=store._reconnect,
        on_exhausted=store._gate.note_rpc_stalled,
    )


def store_rpc_health_probe(store: Any, *, kind: str, name: str) -> bool:
    """RPC-level health probe for a store's ``rpc_stalled`` quarantine recovery.

    A single cheap real RPC (``GetBlobSize`` on a sentinel key that never exists — 0 on a
    healthy daemon, HANGS on a zombie) through :func:`probe_rpc_health`. Returns True iff
    it answers cleanly within the health-probe window; reconnects first so a zombie's
    stale handle is never reused; never raises.
    """
    return probe_rpc_health(
        reconnect=store._reconnect,
        make_probe_call=lambda: store._cte.Tag(kind).GetBlobSize(name),
        port=store._gate.port,
    )
