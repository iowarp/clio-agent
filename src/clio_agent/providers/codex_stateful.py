"""Stateful-delta transport for the native ``codex app-server`` (#891, the TTFT closer).

The codex analog of :mod:`clio_agent.providers.claude_code_stateful`: the *send
side* that turns clio's append-only ReActV2 wire into codex's native delta. The
provider-agnostic core — the delta detector, the typed reset catalog, the
per-forward scope, and the bounded session registry — is shared from
:mod:`clio_agent.providers.stateful_common`; this module owns ONLY what is
codex-specific: opening a PERSISTENT thread as the session handle, keying the
registry on the codex ``effort`` knob, detecting a pool respawn, and the
``provider.stateful`` audit row.

**Why a persistent thread (the shared-prefix fix).** The flag-OFF path runs a fresh
``ephemeral=True`` thread per call, so codex re-ingests the whole prompt every call
(measured ~33% cache, all spawn-amortization) — the automatic prefix cache is capped
by the ~20K prefix codex injects (base instructions + ``AGENTS.md`` + plugins) ahead
of clio's prompt. A persistent (``ephemeral=False``) thread RETAINS the conversation
server-side, so continuing it with only the NEW appended content (the delta) means
codex re-ingests NEITHER that injected prefix NOR the prior turns — the shared-prefix
ceiling is sidestepped entirely, which is the point.

**The engaged flow.** Per expert-forward scope the registry holds ONE persistent
thread on the pooled ``(model, cwd)`` app-server process. Call 1 is a ``full`` send:
:meth:`CodexAppServerProcess.start_thread` opens the thread (``ephemeral=False``) and
:meth:`CodexAppServerProcess.run_turn_on_thread` runs the full prompt on it. Call
N+1, when :func:`~clio_agent.providers.stateful_common.classify_delta` says
``delta``, reuses that thread and runs ONLY the appended body (byte-identical head
growth beneath the static tail). On ANY reset reason (``first_call`` /
``prefix_mismatch`` / ``ops_reset`` / ``session_evicted`` = LRU-eviction OR pool
respawn / ``provider_error``) a FRESH persistent thread is opened and the full prompt
re-sent — typed, recorded, never silent (#775).

**Respawn detection (``session_evicted``).** The pool respawns a dead process on the
next call, which invalidates every thread the old process held. The registry stores
the process object as the entry's opaque ``extra``; :func:`resolve_codex_stateful_send`
compares it to the pool's current process and, on a mismatch, flags the scope
``session_evicted`` so the next send re-opens a thread on the live process.

**Inertness.** Inert unless
a per-forward stateful scope
is active (set only by the ReActV2 loop). Off either, :func:`resolve_codex_stateful_send`
returns a plain full send that never touches the pool or registry — the pre-slice
ephemeral-per-call path runs byte-for-byte unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.providers.stateful_common import (
    StatefulSessionRegistry,
    active_stateful_scope,
    register_scope_registry,
    stateful_reset_payload,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.providers.codex_app_server import CodexAppServerProcess

__all__ = [
    "CodexStatefulSend",
    "codex_stateful_delta_enabled",
    "codex_stateful_registry",
    "resolve_codex_stateful_send",
]


# --------------------------------------------------------------------------- #
# Kill-switch + bound config (codex-specific).
# --------------------------------------------------------------------------- #
def codex_stateful_delta_enabled() -> bool:
    """The codex stateful session-delta transport is the ONLY send semantics.

    The ``CLIO_CODEX_STATEFUL_DELTA`` kill-switch was deleted in the v0.8.0
    cleanup (live acceptance #893: persistent-thread TTFT 2.95s vs 7.37s
    ephemeral, typed reset catalog). Structural gating remains: engages only
    under an active per-forward scope
    (:func:`~clio_agent.providers.stateful_common.active_stateful_scope`).
    """
    return True


def _codex_registry_capacity() -> int:
    """Max live codex stateful-session entries before LRU eviction (default 128).

    Override via ``providers.codex.stateful_capacity`` /
    ``CLIO_CODEX_STATEFUL_CAPACITY``. Clamped to ``>= 1`` by the shared registry.
    """
    try:
        from clio_agent import conf  # noqa: PLC0415

        return int(
            conf.resolve(
                "providers.codex.stateful_capacity",
                env="CLIO_CODEX_STATEFUL_CAPACITY",
                default=128.0,
                cast=conf.as_float,
            )
        )
    except Exception:  # noqa: BLE001 - never let config break a turn
        return 128


# The process-wide codex registry singleton, registered for scope-end teardown so
# the ReActV2 loop's one scope releases this leg too.
_CODEX_REGISTRY = StatefulSessionRegistry(capacity_resolver=_codex_registry_capacity)
register_scope_registry(_CODEX_REGISTRY)


def codex_stateful_registry() -> StatefulSessionRegistry:
    """Return the process-wide codex stateful-session registry singleton."""
    return _CODEX_REGISTRY


# --------------------------------------------------------------------------- #
# The resolved send + its instrumentation.
# --------------------------------------------------------------------------- #
@dataclass
class CodexStatefulSend:
    """A resolved codex send plan the transport executes (delta/full + persistent thread).

    Attributes:
        prompt: The turn input to run — the delta tail (``delta``) or the full prompt
            (``full`` / inert).
        thread_id: The persistent codex thread to continue (stable across a delta run,
            fresh on any reset); ``""`` when not engaged.
        mode: ``"delta"`` or ``"full"``.
        reason: The typed reset reason for a ``full`` send when engaged, else ``None``.
        delta_chars: Characters actually sent this call (``len(prompt)``) — what the
            waterfall attributes TTFT against.
        engaged: Whether the stateful path was active (flag ON + a scope token). When
            ``False`` the transport runs the byte-identical ephemeral-per-call path.
        process: The pooled app-server process the thread lives on (engaged only).
        session_key: The registry key (for :meth:`note_error`), or ``None`` when inert.
        scope_token: The active scope token, or ``None`` when inert.
        call_id: The per-LM-call correlation id the transport reuses for
            ``emit_call_started`` so the ``provider.stateful`` audit row and the
            ``provider.call_started`` / ``raw_event`` TTFT markers join on ONE id.
    """

    prompt: str
    thread_id: str
    mode: str
    reason: str | None
    delta_chars: int
    engaged: bool
    process: CodexAppServerProcess | None = None
    session_key: tuple[Any, ...] | None = None
    scope_token: str | None = None
    call_id: str = ""

    def note_error(self) -> None:
        """Drop the poisoned thread on a mid-flight send failure (no-op if not engaged).

        The retried call then classifies ``provider_error`` and opens a fresh
        persistent thread (bounded by the LM retry layer).
        """
        if self.engaged and self.session_key is not None and self.scope_token is not None:
            _CODEX_REGISTRY.note_provider_error(self.session_key, self.scope_token)


def resolve_codex_stateful_send(
    *,
    messages: list[dict[str, Any]],
    full_prompt: str,
    model: str,
    cwd: str | None,
    effort: str | None,
    serialize: Callable[[list[dict[str, Any]]], str],
    start_timeout: float = 180.0,
    call_index: int = 0,
) -> CodexStatefulSend:
    """Resolve one codex call into a full-or-delta send plan (the transport seam).

    Inert (flag OFF or no active scope) returns a plain full send that never touches
    the pool or registry — byte-for-byte the pre-slice ephemeral-per-call path. When
    engaged it obtains the pooled process, detects a respawn (a changed process for
    the key → a typed ``session_evicted`` reset), asks the shared registry for a
    :class:`~clio_agent.providers.stateful_common.DeltaPlan` (opening a PERSISTENT
    thread on a full send via ``process.start_thread``), serializes the delta tail
    with the caller's own ``serialize`` (so delta bytes match the full path exactly),
    and records a typed ``provider.stateful`` audit row.

    Args:
        messages: The rendered chat-message dicts (the prefix-check operand).
        full_prompt: The already-serialized full prompt (sent on any full/reset).
        model: The clean model id (part of the session key).
        cwd: The transport cwd (part of the session key + the pool key).
        effort: The turn-pinned reasoning effort (part of the session key).
        serialize: The transport's message-list serializer (used for the delta tail).
        start_timeout: The ``thread/start`` deadline used when opening a fresh thread.
        call_index: The provider call index, for the audit row.

    Returns:
        The :class:`CodexStatefulSend` the transport executes.

    Raises:
        CodexAppServerError: If opening a fresh persistent thread fails (the shared
            registry restores the consumed forced reason so the bounded retry
            reclassifies with the SAME typed reason — never a silent ``first_call``).
    """
    # One correlation id per LM call, reused by the transport's emit_call_started so
    # the provider.stateful row and the TTFT markers join on ONE id.
    call_id = uuid.uuid4().hex
    scope = active_stateful_scope()
    if scope is None or not codex_stateful_delta_enabled():
        # Inert: byte-identical pre-slice behaviour (ephemeral thread, full prompt).
        return CodexStatefulSend(
            prompt=full_prompt,
            thread_id="",
            mode="full",
            reason=None,
            delta_chars=len(full_prompt),
            engaged=False,
            call_id=call_id,
        )

    from clio_agent.providers.codex_app_server import _APP_SERVER_POOL  # noqa: PLC0415
    from clio_agent.providers.codex_litellm import _resolve_codex_binary  # noqa: PLC0415

    process = _APP_SERVER_POOL.process_for(
        binary=_resolve_codex_binary(), model=model, cwd=cwd
    )
    session_key = (scope, model, cwd, effort)
    # Respawn detection: if the process backing this key changed (the pool evicted a
    # dead one and spawned fresh), the stored thread died with it — flag the scope so
    # the next send re-opens a thread on the live process. session_evicted covers both
    # LRU eviction and a process respawn (see the shared catalog).
    prior_process = _CODEX_REGISTRY.peek_extra(session_key)
    if prior_process is not None and prior_process is not process:
        _CODEX_REGISTRY.mark_reset(scope, "session_evicted")

    def _open_thread() -> str:
        return process.start_thread(ephemeral=False, timeout=start_timeout)

    plan, thread_id = _CODEX_REGISTRY.plan(
        session_key=session_key,
        scope_token=scope,
        messages=messages,
        open_handle=_open_thread,
        extra=process,
    )
    prompt = serialize(plan.messages) if plan.mode == "delta" else full_prompt
    send = CodexStatefulSend(
        prompt=prompt,
        thread_id=thread_id,
        mode=plan.mode,
        reason=plan.reason,
        delta_chars=len(prompt),
        engaged=True,
        process=process,
        session_key=session_key,
        scope_token=scope,
        call_id=call_id,
    )
    _audit_codex_stateful(
        send, model=model, call_index=call_index, prefix_len=plan.prefix_len, total=len(messages)
    )
    return send


def _audit_codex_stateful(
    send: CodexStatefulSend, *, model: str, call_index: int, prefix_len: int, total: int
) -> None:
    """Emit one ``provider.stateful`` audit row (no-silent-fallback house style).

    A no-op unless ``CLIO_STREAM_AUDIT_LOG`` is configured. Carries the SAME fields as
    the claude leg (``stateful_mode`` / ``delta_chars`` / typed reset ``reason`` /
    ``prefix_messages`` / ``total_messages``), joined to ``emit_call_started`` on
    ``call_id`` so ``scripts/analyze_turn_waterfall.py`` attributes TTFT by mode for
    codex exactly as it does for claude.
    """
    from clio_agent.runtime.stream_audit import stream_audit, stream_audit_enabled  # noqa: PLC0415

    if not stream_audit_enabled():
        return
    row: dict[str, Any] = {
        "provider": "codex_app_server",
        "transport": "app_server",
        "model": f"codex/{model}",
        "call_id": send.call_id,
        "call_index": call_index,
        "stateful_mode": send.mode,
        "delta_chars": send.delta_chars,
        "prefix_messages": prefix_len,
        "total_messages": total,
        "session_id": send.thread_id,
    }
    if send.reason is not None:
        row.update(stateful_reset_payload(send.reason))
    stream_audit("provider.stateful", **row)
