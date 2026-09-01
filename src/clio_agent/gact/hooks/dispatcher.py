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
from clio_agent.gact.hooks.audit import emit_hook_audit, should_audit
from clio_agent.gact.hooks.config import HookEntry, discover_hook_entries
from clio_agent.gact.hooks.events import (
    AFTER_MODEL,
    BEFORE_MODEL,
    KNOWN_EVENTS,
    POST_TOOL_BATCH,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SEMANTIC_EVENT,
    SESSION_END,
    SESSION_START,
    STOP,
    SUBAGENT_START,
    SUBAGENT_STOP,
    USER_PROMPT_SUBMIT,
    is_deny_capable,
)
from clio_agent.gact.hooks.wire import (
    HookDecision,
    HookEnvelope,
    HookInfraError,
    HookOutcome,
    ModelRequest,
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

    def has_hooks_for(self, event: str) -> bool:
        """Return whether ANY enabled entry declares ``event`` in its ``on`` set.

        A cheap, envelope-free pre-check (no ``match`` evaluation): the ``dspy.LM``
        wrapper uses it to stay pure pass-through when no model hook is configured
        (zero overhead, identical output), only paying the dispatch cost when a
        ``BeforeModel``/``AfterModel`` hook actually exists.
        """

        with self._lock:
            entries = list(self._entries)
        return any(entry.enabled and entry.is_trusted and event in entry.on for entry in entries)

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
                "scope": entry.scope,
                "trust": entry.trust,
                "run_type": entry.run.type,
            }
            if adapter is None:  # pragma: no cover - default_adapters covers every declared type
                record["status"] = "error"
                record["error"] = f"no adapter for run.type {entry.run.type!r}"
                self._audit(event, envelope, record)
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
                self._audit(event, envelope, record)
                records.append(record)
                continue
            record["status"] = "denied" if decision.decision == "deny" else "completed"
            record["decision"] = decision.decision
            if decision.reason:
                record["reason"] = decision.reason
            self._audit(event, envelope, record)
            decisions.append(decision)
            records.append(record)
        return HookOutcome.merge(decisions, records)

    @staticmethod
    def _audit(event: str, envelope: HookEnvelope, record: dict[str, Any]) -> None:
        """Audit ONE hook invocation on the semantic highway (P2.7), exactly once.

        Every branch of :meth:`dispatch` (a decision, a denial, an infra error/timeout,
        a pre-execution rejection carried as a ``deny``) routes here so the audit is
        complete. ``SemanticEvent``-event invocations are skipped to avoid the
        highway-recursion the observation hooks would otherwise cause (see
        :mod:`clio_agent.gact.hooks.audit`). Never raises — audit must not crash a
        dispatch it observes.
        """

        if not should_audit(event):
            return
        emit_hook_audit(
            {
                **record,
                "session_id": envelope.session_id,
                "turn_id": envelope.turn_id,
                "tool_name": envelope.tool_name or "",
            }
        )

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
        # Only ENABLED and TRUSTED entries actually dispatch (mirrors ``matching``'s
        # ``runs_for`` filter): a disabled or content-untrusted (P2.7) entry is declared
        # but never fires, so it must not inflate the capability a caller relies on.
        active = [entry for entry in entries if entry.enabled and entry.is_trusted]
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

    def inspect(self) -> list[dict[str, Any]]:
        """Return a read-only per-hook view for ``GET /v1/hooks`` (P2.7 introspection).

        Lists EVERY loaded hook (including disabled and content-untrusted ones — the
        whole point of the debugging surface is to SEE the hook a changed fingerprint
        just stopped running), each with its stable ``id``, the events it runs ``on``,
        its ``match`` predicate, its source ``scope`` label (user/project/managed), its
        content ``trust`` state, and whether it is ``enabled``.
        """

        with self._lock:
            entries = list(self._entries)
        rows: list[dict[str, Any]] = []
        for entry in entries:
            match = entry.match
            rows.append(
                {
                    "id": entry.id,
                    "on": sorted(entry.on),
                    "match": {
                        "tool": match.tool.pattern if match.tool is not None else None,
                        "annotations": dict(match.annotations),
                        "argsPattern": (
                            match.args_pattern.pattern if match.args_pattern is not None else None
                        ),
                    },
                    "run_type": entry.run.type,
                    "source": entry.scope or "unknown",
                    "source_path": entry.source,
                    "trust": entry.trust,
                    "fingerprint": entry.fingerprint,
                    "enabled": entry.enabled,
                    "runs": entry.enabled and entry.is_trusted,
                }
            )
        return rows


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
    """Build the configured dispatcher from the managed + user + project config files.

    Config knobs (file → env → default, via ``conf.resolve``):

    * ``hooks.config`` / ``CLIO_HOOKS_CONFIG`` — a single explicit file that overrides
      the user+project discovery paths (tests / single-file deployments).
    * ``hooks.managed_config`` / ``CLIO_HOOKS_MANAGED_CONFIG`` — the admin/managed hook
      file (highest precedence; no default location — managed hooks are opt-in).
    * ``hooks.allow_managed_only`` / ``CLIO_HOOKS_ALLOW_MANAGED_ONLY`` — the
      ``allowManagedHooksOnly`` lockdown: drop every non-managed source.
    * ``hooks.trust_store`` / ``CLIO_HOOKS_TRUST_STORE`` — override the trusted-
      fingerprint store path (default: colocated with the hook config).

    Every LOADED hook is then trust-evaluated (:mod:`clio_agent.gact.hooks.trust`): a
    hook whose content fingerprint changed since it was last trusted is marked
    ``untrusted`` and will NOT run (no silent run of changed content).
    """

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load
    from clio_agent.gact.hooks.trust import evaluate_trust, trust_store_path_for  # noqa: PLC0415

    explicit = conf.resolve(
        "hooks.config", env="CLIO_HOOKS_CONFIG", default="", cast=conf.as_str
    ).strip()
    managed = conf.resolve(
        "hooks.managed_config", env="CLIO_HOOKS_MANAGED_CONFIG", default="", cast=conf.as_str
    ).strip()
    allow_managed_only = conf.resolve(
        "hooks.allow_managed_only",
        env="CLIO_HOOKS_ALLOW_MANAGED_ONLY",
        default=False,
        cast=conf.as_bool,
    )
    managed_path = Path(managed).expanduser() if managed else None
    if explicit:
        path = Path(explicit).expanduser()
        entries = discover_hook_entries(
            user_config_path=path,
            project_config_path=path,
            managed_config_path=managed_path,
            allow_managed_only=allow_managed_only,
        )
        store_path = trust_store_path_for(path)
    else:
        from clio_agent import paths  # noqa: PLC0415 - avoid import cycle at module load

        entries = discover_hook_entries(
            cwd=cwd, managed_config_path=managed_path, allow_managed_only=allow_managed_only
        )
        store_path = paths.user_config_dir() / "hooks.trust.json"
    override = conf.resolve(
        "hooks.trust_store", env="CLIO_HOOKS_TRUST_STORE", default="", cast=conf.as_str
    ).strip()
    if override:
        store_path = Path(override).expanduser()
    entries = evaluate_trust(entries, store_path=store_path)
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
    """Fire ``Stop`` (post-turn) hooks and return the merged outcome.

    A Stop hook ``deny`` is a BOUNDED completion-gate block ("not done — re-drive"),
    evaluated by :func:`clio_agent.gact.hooks.stop_loop.run_stop_hooks` at the turn-
    finalize boundary — this dispatch itself only fires the hooks and merges. Stop is
    NOT in :data:`DENY_CAPABLE_EVENTS`, so a Stop-hook infra failure is non-blocking
    (typed-recorded, never a fail-closed re-drive).
    """

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


def dispatch_post_tool(
    name: str,
    args: Mapping[str, Any],
    *,
    observation: Any,
    is_error: bool,
    synthetic: bool,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
    context: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``PostToolUse`` hooks after a tool result (P2.3).

    NOT a blocking gate — the effect already ran. A hook may observe, rewrite the
    observation (``updatedToolOutput``, carried on ``outcome.updated_output``), or
    ``deny`` to feed the reason back to the model. Fires on a synthesized result
    too, flagged ``synthetic: true`` in the envelope payload.
    """

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=POST_TOOL_USE,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        tool_name=name,
        tool_input=dict(args),
        tool_annotations=_envelope_tool_annotations(name, context),
        payload={"is_error": bool(is_error), "synthetic": bool(synthetic)},
    )
    return dispatcher.dispatch(POST_TOOL_USE, envelope)


def dispatch_post_tool_batch(
    payload: Mapping[str, Any],
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
) -> HookOutcome:
    """Fire ``PostToolBatch`` observation hooks after a turn's tool round resolves."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=POST_TOOL_BATCH,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        payload=dict(payload),
    )
    return dispatcher.dispatch(POST_TOOL_BATCH, envelope)


def dispatch_session_start(
    *,
    session_id: str = "",
    cwd: str = "",
    payload: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``SessionStart`` observation hooks when a session is created."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=SESSION_START,
        session_id=session_id,
        cwd=cwd,
        payload=dict(payload or {}),
    )
    return dispatcher.dispatch(SESSION_START, envelope)


def dispatch_session_end(
    *,
    session_id: str = "",
    cwd: str = "",
    payload: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``SessionEnd`` observation hooks when a session is closed."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=SESSION_END,
        session_id=session_id,
        cwd=cwd,
        payload=dict(payload or {}),
    )
    return dispatcher.dispatch(SESSION_END, envelope)


def dispatch_subagent_start(
    *,
    session_id: str = "",
    cwd: str = "",
    payload: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``SubagentStart`` observation hooks when a child turn begins."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=SUBAGENT_START,
        session_id=session_id,
        cwd=cwd,
        payload=dict(payload or {}),
    )
    return dispatcher.dispatch(SUBAGENT_START, envelope)


def dispatch_subagent_stop(
    *,
    session_id: str = "",
    cwd: str = "",
    payload: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``SubagentStop`` observation hooks when a child turn reaches terminal."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=SUBAGENT_STOP,
        session_id=session_id,
        cwd=cwd,
        payload=dict(payload or {}),
    )
    return dispatcher.dispatch(SUBAGENT_STOP, envelope)


def dispatch_pre_compact(
    *,
    session_id: str = "",
    cwd: str = "",
    payload: Mapping[str, Any] | None = None,
) -> HookOutcome:
    """Fire ``PreCompact`` observation hooks before a transcript is compacted."""

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=PRE_COMPACT,
        session_id=session_id,
        cwd=cwd,
        payload=dict(payload or {}),
    )
    return dispatcher.dispatch(PRE_COMPACT, envelope)


def model_hooks_active() -> bool:
    """Return whether ANY ``BeforeModel``/``AfterModel`` hook is configured (P2.4).

    The ``dspy.LM`` wrapper's pass-through fast path: when this is ``False`` the
    wrapper is never installed, so a turn with no model hook pays zero overhead and
    produces byte-identical output to today.
    """

    dispatcher = _GLOBAL
    if dispatcher is None:
        return False
    return dispatcher.has_hooks_for(BEFORE_MODEL) or dispatcher.has_hooks_for(AFTER_MODEL)


def dispatch_before_model(
    request: ModelRequest,
    *,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
) -> HookOutcome:
    """Fire ``BeforeModel`` hooks for one outgoing model request (P2.4).

    Deny-capable — the wrapper enforces a ``deny`` (block), a ``synthesize`` (skip
    the real LM with the canned ``llm_response``), and a ``modify`` (``model_override``
    route / ``request_patch`` redact).
    """

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=BEFORE_MODEL,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        model_request=request,
    )
    return dispatcher.dispatch(BEFORE_MODEL, envelope)


def dispatch_after_model(
    request: ModelRequest,
    *,
    response: Any,
    synthetic: bool,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
) -> HookOutcome:
    """Fire ``AfterModel`` hooks for one model response (P2.4).

    NOT a blocking gate — the call already ran. A hook may observe or REWRITE the
    response entering context (``llm_response``). Fires on a synthesized response
    too, flagged ``synthetic: true`` in the envelope payload.
    """

    dispatcher = _GLOBAL
    if dispatcher is None:
        return HookOutcome()
    envelope = HookEnvelope(
        hook_event_name=AFTER_MODEL,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
        model_request=request,
        payload={"synthetic": bool(synthetic), "response": response},
    )
    return dispatcher.dispatch(AFTER_MODEL, envelope)
