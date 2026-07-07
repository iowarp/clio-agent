"""Ambient-LM guard for the GACT runtime (per-expert provider/LM sweep, #818).

Several leaf runtime helpers read the process-wide ``dspy.settings.lm`` to decide
which model to meter, compact, or probe: token accounting
(:mod:`clio_agent.gact.runtime.context_tokens`), usage rollup
(:mod:`clio_agent.gact.usage`), reasoning capture
(:mod:`clio_agent.gact.runtime.globals`), auto-compaction summarisation
(:mod:`clio_agent.gact.agents.runtime`) and the model-id probe
(:func:`clio_agent.gact.providers.config._current_lm_model_id`).

During a turn that read resolves the *actively bound* expert/main LM, because
every expert ``forward`` and the main agent enter
``with dspy.context(lm=..., adapter=...)``. Outside any such context the same read
silently resolves the process **boot-default** LM (``dspy.configure``). The
per-expert-provider design removes reliance on that global default, so an ambient
read must not break invisibly.

This module makes the ambient case *queryable* instead of silent (the
no-silent-fallback ground rule): :func:`active_lm` reports whether a per-profile
``dspy.context`` is bound, and :func:`resolve_active_lm` records a structured
``ambient_lm_default`` reason (built from the dedicated sibling reason-catalog in
:mod:`clio_agent.gact.streaming`, ``_ambient_lm_fallback_payload`` — kept out of
the audited client-facing ``stream_fallback`` set) whenever a call site falls
through to the boot default.

Detection is exact and needs no cooperation from the binding sites. DSPy's
``thread_local_overrides`` ContextVar is empty exactly when no ``dspy.context`` is
active, and carries ``lm`` inside one (``dspy.context`` seeds its overrides from
``main_thread_config`` on entry). So ``"lm" not in overrides`` means the
``dspy.settings.lm`` read resolved the ambient boot default.

The reason is recorded into a dedicated, bounded per-app ledger
(``app.state.ambient_lm_fallbacks``) rather than the single-slot streaming ledger
(``app.state.stream_fallback_reasons``): the usage/token call sites run at
turn-end *outside* any context (ambient by nature), and the turn handler pops the
streaming single slot to report why live streaming fell back — writing the
ambient reason there would clobber that attribution. The ledger keeps the same
catalog payload shape so the miss stays queryable after the fact.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# The single catalog reason for an ambient boot-default LM read. Defined in the
# dedicated sibling catalog
# (``clio_agent.gact.streaming._AMBIENT_LM_FALLBACK_REASON_DEFINITIONS``) so
# ``_ambient_lm_fallback_payload`` validates + shapes it like the stream_fallback
# reasons, without polluting the audited client-facing streaming set.
AMBIENT_LM_FALLBACK_REASON = "ambient_lm_default"

# Cap the per-session ledger so a long-lived session cannot grow it without bound;
# consecutive same-site records are de-duplicated before this cap is consulted.
_MAX_LEDGER_ENTRIES = 64


def _context_lm_bound() -> bool:
    """Return True when the active ``dspy.context`` binds a *distinct* ``lm`` (an expert/main profile).

    ``dspy.settings.lm`` resolves ``thread_local_overrides['lm']`` when a context is
    active and ``main_thread_config['lm']`` (the boot default) otherwise.

    A mere ``"lm" in overrides`` test is **not** sufficient: ``dspy.context`` seeds
    its overrides dict from ``main_thread_config`` (``{**main_thread_config,
    **original_overrides, **kwargs}``), and ``main_thread_config`` *always* carries
    the ``lm`` key (default ``None``). So ``"lm" in overrides`` is True inside *any*
    ``dspy.context(...)`` — even one that binds only an ``adapter`` and no LM. Such a
    context still reads the boot-default LM, which is precisely the ambient,
    silently-boot-default read this guard exists to catch (no-silent-fallback rule).

    The exact signal is **object identity**: a genuinely LM-binding context makes
    ``overrides['lm']`` a *different object* from the boot default
    (``main_thread_config['lm']``). When no LM was bound (or a nested context inherits
    the outer bind), ``overrides['lm']`` is the very same object dspy seeded from the
    default — that is an ambient read. Nested contexts inherit the outer override, so
    an adapter-only inner context under a real ``lm`` bind stays correctly "bound".
    """
    try:
        from dspy.dsp.utils.settings import (  # noqa: PLC0415
            main_thread_config,
            thread_local_overrides,
        )
    except Exception:  # noqa: BLE001 - dspy shape drift must not break metering
        return False
    try:
        overrides = thread_local_overrides.get()
        if "lm" not in overrides:
            return False
        # Bound only when the override LM is a distinct object from the boot default;
        # equal identity means the context merely inherited the ambient default.
        return overrides.get("lm") is not main_thread_config.get("lm")
    except Exception:  # noqa: BLE001
        return False


def install_process_default_lm(lm: Any, adapter: Any = None) -> bool:
    """Install/refresh the process-default dspy LM (+ adapter) — the admin bind's job.

    The default/admin bind (``PUT /v1/providers/lm``) is the ONLY writer of the
    process-global default (design ``per-expert-provider-lm.md`` §6): experts still
    resolve their own LM per ``dspy.context``, but the ambient consumers this module
    guards (auto-compaction summarisation, usage/token metering, the turn-end
    model-id probe) and the deferred-boot / rebind paths need a *valid, current*
    default to read when no per-profile context is active. Setting it on the first
    bind fixes the deferred-boot ``lm=None`` hard-503; refreshing it on a rebind
    fixes the stale-model ambient read.

    Writes ``dspy.dsp.utils.settings.main_thread_config`` **directly** instead of
    calling :func:`dspy.configure`. The bind runs on an executor worker thread that
    is not the boot-time ``dspy.configure`` owner thread, and dspy rejects a
    second-owner ``configure`` with ``RuntimeError``. The direct write is a plain
    per-key dict assignment (atomic under the GIL) and is exactly the slot
    ``dspy.settings.lm`` resolves when no ``dspy.context`` override is active
    (mirroring :func:`_context_lm_bound`). Installing the adapter alongside the LM
    keeps the ambient ChatAdapter matched to the ambient LM (never a mismatched
    pair).

    Args:
        lm: The new process-default dspy LM. ``None`` is a no-op (nothing to install).
        adapter: The adapter to install alongside it; left unchanged when ``None``.

    Returns:
        ``True`` when the default LM was installed; ``False`` when ``lm`` is ``None``
        or dspy's settings module is unavailable.
    """
    if lm is None:
        return False
    try:
        from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - dspy shape drift must not break the bind
        logger.warning("process-default LM install skipped: dspy settings module unavailable")
        return False
    main_thread_config["lm"] = lm
    if adapter is not None:
        main_thread_config["adapter"] = adapter
    return True


def active_lm() -> tuple[Any, bool]:
    """Return ``(lm, ambient)``: the active dspy LM and whether it is the boot default.

    ``ambient`` is True when no per-profile ``dspy.context`` is bound, so the read
    resolved the process boot-default LM (``dspy.configure``) rather than an
    expert/main profile. ``lm`` may be ``None`` when dspy is unavailable or nothing
    has been configured yet.
    """
    try:
        import dspy  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - metering is best-effort
        return None, True
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    return lm, not _context_lm_bound()


def resolve_active_lm(
    *,
    site: str,
    explicit: Any = None,
    app: Optional["FastAPI"] = None,
    sid: str = "",
) -> Any:
    """Resolve the LM a runtime helper should use, flagging an ambient fallback.

    Preference order (no-silent-fallback):

    1. ``explicit`` — an LM the caller passes in (a bound profile; never ambient).
    2. the LM bound by the active profile's ``dspy.context`` (expert/main).
    3. the process boot-default LM — recorded as an ``ambient_lm_default`` reason so
       the miss is queryable, never silent.

    Args:
        site: A stable identifier for the call site (``module.function``) recorded
            as the reason ``message`` so a query pinpoints which consumer went ambient.
        explicit: An LM the caller already resolved explicitly; returned as-is.
        app: The FastAPI app to attribute the reason to; resolved from the runtime
            context when omitted.
        sid: The session id to attribute the reason to; resolved from the runtime
            context when omitted.

    Returns:
        The LM to use (may be ``None`` when nothing is configured).
    """
    if explicit is not None:
        return explicit
    lm, ambient = active_lm()
    if ambient and lm is not None:
        record_ambient_fallback(site, app=app, sid=sid)
    return lm


def ambient_lm_fallbacks(app: "FastAPI") -> dict[str, list[dict[str, Any]]]:
    """Return the per-session ambient-LM fallback ledger, creating it on first use.

    Keyed by session id, each value a list of catalog payloads (most-recent last).
    The dedicated ledger keeps ambient records off the single-slot streaming ledger
    the turn handler pops for its ``stream_fallback`` metadata.
    """
    ledger = getattr(app.state, "ambient_lm_fallbacks", None)
    if not isinstance(ledger, dict):
        ledger = {}
        app.state.ambient_lm_fallbacks = ledger
    return ledger


def record_ambient_fallback(
    site: str,
    *,
    app: Optional["FastAPI"] = None,
    sid: str = "",
) -> None:
    """Record a structured ``ambient_lm_default`` reason for a boot-default LM read.

    Builds the reason from the dedicated sibling catalog (via
    ``_ambient_lm_fallback_payload``) and appends it to the per-app ambient ledger
    so the miss is queryable. Consecutive records for the same ``site`` in a session
    are collapsed, and the ledger is capped, so a per-turn ambient consumer cannot
    grow it without bound. When no session context is reachable the fallback is at
    least logged (never fully silent)."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    if app is None:
        app = _ctx.active_app()
    if not sid:
        sid = _ctx.active_session_id()
    if app is None or not sid:
        logger.warning(
            "ambient LM boot-default resolved off-session site=%s reason=%s",
            site,
            AMBIENT_LM_FALLBACK_REASON,
        )
        return
    try:
        from clio_agent.gact.streaming import _ambient_lm_fallback_payload  # noqa: PLC0415

        payload = _ambient_lm_fallback_payload(AMBIENT_LM_FALLBACK_REASON, message=site)
        entries = ambient_lm_fallbacks(app).setdefault(sid, [])
        # Collapse consecutive same-site records so a per-turn ambient consumer
        # leaves ONE queryable entry per site instead of one per turn.
        if not entries or entries[-1].get("message") != site:
            entries.append(payload)
        if len(entries) > _MAX_LEDGER_ENTRIES:
            del entries[:-_MAX_LEDGER_ENTRIES]
    except Exception:  # noqa: BLE001 - recording is observability, never fatal
        logger.warning(
            "ambient LM fallback record failed site=%s reason=%s",
            site,
            AMBIENT_LM_FALLBACK_REASON,
            exc_info=True,
        )
