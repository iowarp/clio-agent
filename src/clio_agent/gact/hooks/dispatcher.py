"""The one hook dispatcher (P2.2).

:class:`HookDispatcher` owns matching, ordering, the per-hook failure posture, and
merging into a single :class:`~clio_agent.gact.hooks.wire.HookOutcome`. Transports
are adapters behind one interface (:mod:`clio_agent.gact.hooks.adapters`); the
per-hook timeout is enforced by the adapter (native subprocess timeout).

The four live consumers of the deleted ``runtime/hooks.py`` fire through the thin
module-level helpers here (``dispatch_pre_tool`` / ``dispatch_user_prompt_submit``
/ ``dispatch_stop`` / ``dispatch_semantic_event``), which resolve the
process-global dispatcher installed by ``build_app`` — the same wiring shape the
old registry used (process-global + ``app.state`` metadata), so no new store
appears (RULE 4).

Invariants enforced here (hooks-research §5.5):
* stable ``id`` keys every hook (identity, provenance, state) — never positional;
* a hook may only TIGHTEN — its ``allow`` never lifts a caller's deny (the tool
  gate consults hooks and its own policy independently; only a hook ``deny`` adds
  restriction);
* a hook FAILURE is distinct from a user rejection — an infra failure raises a
  typed error and, for a deny-capable hook, denies fail-closed with a message that
  says so;
* reads are never gated — this dispatcher fires PreToolUse only AFTER the gate's
  ``is_read_only`` fast-allow (enforced at the call site in ``permission_gate``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.gact.hooks.adapters import HookAdapter, default_adapters
from clio_agent.gact.hooks.config import HookEntry, discover_hook_entries
from clio_agent.gact.hooks.events import (
    KNOWN_EVENTS,
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    STOP,
    USER_PROMPT_SUBMIT,
    is_deny_capable,
)
from clio_agent.gact.hooks.wire import (
    HookDecision,
    HookEnvelope,
    HookInfraError,
    HookOutcome,
    record_hook_reason,
    wire_annotations,
)

logger = logging.getLogger(__name__)

# NOTE: "declarative" is not yet a member of the x_clio_hook_backend enum documented
# in the external gact-tui SPEC.md. That is a tracked cross-repo contract-sweep item
# (the SPEC needs updating to match this backend), not something to silently paper
# over here — do NOT rename/alias this value to fit the stale enum.
_BACKEND_NAME = "declarative"


class HookDispatcher:
    """Match, run, and merge hooks for one event."""

    backend_name = _BACKEND_NAME

    def __init__(
        self,
        entries: list[HookEntry] | None = None,
        *,
        adapters: Mapping[str, HookAdapter] | None = None,
    ) -> None:
        # Stable ordering by id so runs are deterministic and identity is positional-free.
        self._entries: list[HookEntry] = sorted(entries or [], key=lambda e: e.id)
        self._adapters: Mapping[str, HookAdapter] = dict(adapters or default_adapters())
        self._lock = threading.Lock()

    @property
    def entries(self) -> list[HookEntry]:
        return list(self._entries)

    def matching(self, event: str, envelope: HookEnvelope) -> list[HookEntry]:
        """Return the enabled hooks that run for ``event`` given ``envelope``."""

        with self._lock:
            entries = list(self._entries)
        return [entry for entry in entries if entry.runs_for(event, envelope)]

    def dispatch(self, event: str, envelope: HookEnvelope) -> HookOutcome:
        """Run every matching hook for ``event`` and merge to one outcome.

        A hook infra failure is resolved by the per-hook fail-closed posture: for a
        deny-capable event a ``failClosed`` hook denies (with a typed, non-"user
        rejected" reason); otherwise it is non-blocking. Observation events can
        never block, so an infra failure there is always non-blocking.
        """

        deny_capable = is_deny_capable(event)
        decisions: list[HookDecision] = []
        records: list[dict[str, Any]] = []
        for entry in self.matching(event, envelope):
            adapter = self._adapters.get(entry.run.type)
            record: dict[str, Any] = {
                "hook_id": entry.id,
                "event": event,
                "source": entry.source,
                "run_type": entry.run.type,
            }
            if adapter is None:  # pragma: no cover - default_adapters covers every declared type
                record["status"] = "error"
                record["error"] = f"no adapter for run.type {entry.run.type!r}"
                records.append(record)
                continue
            try:
                decision = adapter.invoke(entry, envelope)
            except HookInfraError as exc:
                record["status"] = "error"
                record["error"] = str(exc)
                record["reason"] = exc.reason
                if deny_capable and entry.fail_closed:
                    record_hook_reason(
                        "hook_fail_closed_deny",
                        hook_id=entry.id,
                        event=event,
                        infra_reason=exc.reason,
                    )
                    record["decision"] = "deny"
                    decisions.append(
                        HookDecision(
                            decision="deny",
                            reason=(
                                f"Hook {entry.id!r} failed ({exc.reason}) and is fail-closed; "
                                f"the call was blocked. This is a hook infrastructure failure, "
                                f"not a user rejection."
                            ),
                            hook_id=entry.id,
                        )
                    )
                records.append(record)
                continue
            record["status"] = "denied" if decision.decision == "deny" else "completed"
            record["decision"] = decision.decision
            if decision.reason:
                record["reason"] = decision.reason
            decisions.append(decision)
            records.append(record)
        return HookOutcome.merge(decisions, records)

    def metadata(self) -> dict[str, Any]:
        """Return capability metadata for ``/v1/capabilities`` (x_clio_hook_*).

        Only ENABLED entries are counted/listed here — this report describes what
        actually runs (mirrors :meth:`matching`'s ``entry.enabled`` filter), not
        the raw config. A ``enabled: false`` entry is declared but never
        dispatches, so it must not inflate ``hook_count``/``handler_counts``/
        ``hook_ids`` as a capability a caller could rely on being invoked.
        """

        with self._lock:
            entries = list(self._entries)
        active = [entry for entry in entries if entry.enabled]
        counts: dict[str, int] = dict.fromkeys(sorted(KNOWN_EVENTS), 0)
        for entry in active:
            for event in entry.on:
                counts[event] = counts.get(event, 0) + 1
        return {
            "backend": self.backend_name,
            "enabled": True,
            "hook_count": len(active),
            "handler_counts": counts,
            "hook_ids": [entry.id for entry in active],
        }


# --------------------------------------------------------------------------- #
# Process-global dispatcher (installed by build_app) + thin consumer helpers.    #
# Same wiring shape as the deleted registry: a global + app.state metadata, so no #
# fifth store is introduced (RULE 4).                                            #
# --------------------------------------------------------------------------- #
_GLOBAL: HookDispatcher | None = None


def install_global_dispatcher(dispatcher: HookDispatcher | None) -> None:
    """Install (or clear) the process-global hook dispatcher."""

    global _GLOBAL
    _GLOBAL = dispatcher


def get_global_dispatcher() -> HookDispatcher | None:
    """Return the installed process-global dispatcher, or ``None``."""

    return _GLOBAL


def build_hook_dispatcher(*, cwd: Path | None = None) -> HookDispatcher:
    """Build the configured dispatcher from the user + project config files.

    ``CLIO_HOOKS_CONFIG`` overrides the discovery paths with a single explicit
    file (used by tests and single-file deployments).
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    explicit = conf.resolve(
        "hooks.config", env="CLIO_HOOKS_CONFIG", default="", cast=conf.as_str
    ).strip()
    if explicit:
        path = Path(explicit).expanduser()
        entries = discover_hook_entries(user_config_path=path, project_config_path=path)
    else:
        entries = discover_hook_entries(cwd=cwd)
    return HookDispatcher(entries)


def _envelope_tool_annotations(name: str, context: Mapping[str, Any] | None) -> dict[str, bool]:
    """Derive the wire ``tool_annotations`` block for a tool call.

    External-MCP calls carry declared annotations in the gate ``context``; built-ins
    are looked up in the catalog's annotation source of truth (P0.3). Absent
    evidence is fail-safe (destructive/openWorld true) via :func:`wire_annotations`.
    """

    if isinstance(context, Mapping) and context.get("annotations") is not None:
        return wire_annotations(context.get("annotations"))
    from clio_agent.tools.catalog import _BUILTIN_ANNOTATIONS  # noqa: PLC0415

    return wire_annotations(_BUILTIN_ANNOTATIONS.get(name))


def dispatch_pre_tool(
    name: str,
    args: Mapping[str, Any],
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
    context: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``PreToolUse`` hooks. Deny-capable — the caller enforces the deny."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=PRE_TOOL_USE,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        tool_name=name,
        tool_input=dict(args),
        tool_annotations=_envelope_tool_annotations(name, context),
    )
    return dispatcher.dispatch(PRE_TOOL_USE, envelope)


def dispatch_user_prompt_submit(
    text: str,
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
) -> HookOutcome:
    """Fire ``UserPromptSubmit`` hooks. Deny-capable — a deny vetoes the turn."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=USER_PROMPT_SUBMIT,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        prompt=text,
    )
    return dispatcher.dispatch(USER_PROMPT_SUBMIT, envelope)


def dispatch_stop(
    payload: Mapping[str, Any],
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
) -> HookOutcome:
    """Fire ``Stop`` (post-turn) observation hooks. Never blocks in this slice."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=STOP,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        payload=dict(payload),
    )
    return dispatcher.dispatch(STOP, envelope)


def dispatch_semantic_event(
    payload: Mapping[str, Any],
    *,
    session_id: str = "",
) -> HookOutcome:
    """Fire ``SemanticEvent`` observation hooks. Never blocks."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=SEMANTIC_EVENT,
        session_id=session_id,
        payload=dict(payload),
    )
    return dispatcher.dispatch(SEMANTIC_EVENT, envelope)
