"""Which model a SPOTTER watcher runs on, as the person chose it.

Resolution, first match wins:

1. the watched session's ``metadata.spotter_model``: a fleet reference
   (``account/model``, e.g. ``server-vllm-spotter/Qwen3-8B``, or a bare model id);
2. the server default, ``fleet.spotter`` (``GET``/``PATCH /v1/settings/fleet``,
   field ``spotter``: ``{provider_id, model_id}`` or null);
3. nothing: the watcher inherits the watched session's model, as before.

A chosen model is passed as the watcher spawn's ``model`` and recorded with
source ``"person"``: the person picked it, so the spending guard never asks
again. It is checked against the fleet when the session arms (and re-arms on a
changed choice), so an unknown or unavailable model is a typed 422 with the
transition rolled back, never a half-armed session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Union, cast

from clio_agent.gact.fleet.selection import ModelSelectionRefused

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.sessions import Session as StoredSession
    from clio_agent.gact.spotter_arming import WatcherArmingRefusal
    from clio_agent.gact.types import Session as WireSession

    #: A route hands the stored row; the watcher module annotates the wire type.
    Session = Union[StoredSession, WireSession]

logger = logging.getLogger(__name__)

#: Session-metadata key naming the watcher's model (a fleet reference string).
SPOTTER_MODEL_KEY = "spotter_model"


def default_spotter_ref() -> str:
    """The server-default SPOTTER model as ``account/model``, or ``""`` to inherit."""

    from clio_agent.gact.fleet_settings import fleet_settings  # noqa: PLC0415

    try:
        choice = fleet_settings()["spotter"]
    except ValueError as exc:
        # A malformed judge setting is reported where it is edited; it does not
        # stop a watcher from arming on the watched session's model.
        logger.warning("spotter_default_model_unreadable reason=%s -- inheriting", exc)
        return ""
    return f"{choice['provider_id']}/{choice['model_id']}" if choice else ""


def session_spotter_ref(session: "Session") -> Any:
    """The raw ``spotter_model`` the session carries (validated by :func:`model_refusal`)."""

    return (getattr(session, "metadata", None) or {}).get(SPOTTER_MODEL_KEY)


def watcher_model_ref(app: "FastAPI", session: "Session") -> str:
    """The model reference the session's watcher runs on; ``""`` inherits."""

    del app  # The default lives in user configuration, not on the app.
    own = session_spotter_ref(session)
    if isinstance(own, str) and own.strip():
        return own.strip()
    return default_spotter_ref()


def is_person_choice(app: "FastAPI", parent: "Session", agent_id: str, model: str) -> bool:
    """Whether ``model`` on a spawn of ``agent_id`` is the watcher model the person chose."""

    from clio_agent.gact.spotter_watcher import (  # noqa: PLC0415
        SPOTTER_APPROVAL_MODE,
        _watcher_agent_ref,
    )

    return bool(
        model.strip()
        and parent.approval_mode == SPOTTER_APPROVAL_MODE
        and agent_id == _watcher_agent_ref()
        and model.strip() == watcher_model_ref(app, parent)
    )


def _warm_catalog(app: "FastAPI") -> None:
    """Read the provider catalog once if the server has not, from a route worker thread."""

    from clio_agent.gact.fleet.child_model import warm_model_catalog  # noqa: PLC0415

    if isinstance(getattr(app.state, "provider_catalog", None), dict):
        return
    asyncio.run_coroutine_threadsafe(
        warm_model_catalog(app), app.state.conversations._loop
    ).result()


def model_refusal(app: "FastAPI", session: "Session") -> "WatcherArmingRefusal | None":
    """Check the session's watcher model against the fleet; the typed refusal, if any.

    Runs the same choice the watcher spawn makes (the watcher agent's
    requirements, source ``"person"``), so a model that passes here is the
    model it runs on. ``None`` when no model is chosen (inherit) or it can run.
    """

    from clio_agent.gact.fleet.child_model import (  # noqa: PLC0415
        choose_child_model,
        fleet_entries,
    )
    from clio_agent.gact.spawn_context import resolve_child_agent  # noqa: PLC0415
    from clio_agent.gact.spotter_arming import (  # noqa: PLC0415
        REFUSAL_WATCHER_MODEL_REFUSED,
        WatcherArmingRefusal,
    )
    from clio_agent.gact.spotter_watcher import _watcher_agent_ref  # noqa: PLC0415

    agent_ref = _watcher_agent_ref()
    own = session_spotter_ref(session)
    if own is not None and not isinstance(own, str):
        return WatcherArmingRefusal(
            reason=REFUSAL_WATCHER_MODEL_REFUSED,
            detail=f"metadata.{SPOTTER_MODEL_KEY} must be a model reference string or null",
            agent_ref=agent_ref,
            server="",
            extra={"code": "model_invalid", "model": own},
        )
    ref = watcher_model_ref(app, session)
    if not ref:
        return None
    try:
        child, _pin = resolve_child_agent(app, session.id, agent_ref)
    except ValueError:
        # The watcher agent itself does not resolve: the MCP gate owns that refusal.
        return None
    try:
        _warm_catalog(app)
        choose_child_model(
            app,
            cast("StoredSession", session),
            child,
            explicit=ref,
            same_agent=False,
            chooser="person",
        )
    except ModelSelectionRefused as refusal:
        available = sorted(
            entry.ref for entry in fleet_entries(app, needed=False) if entry.available
        )
        return WatcherArmingRefusal(
            reason=REFUSAL_WATCHER_MODEL_REFUSED,
            detail=str(refusal),
            agent_ref=agent_ref,
            server="",
            remedy=(
                f"set metadata.{SPOTTER_MODEL_KEY} (or the fleet setting 'spotter') to one "
                "of the available models, or clear it to use the watched session's model"
            ),
            extra={"code": refusal.code, "model": ref, "available": available},
        )
    return None


def rearm_on_model_change(
    app: "FastAPI",
    session: "WireSession",
    *,
    prior_approval_mode: str,
    prior_spotter_model: Any,
) -> None:
    """Route hook: re-arm the watcher when its model changes on an armed session.

    Only a session that was and stays in spotter-ai and whose
    ``metadata.spotter_model`` changed is touched (entering or leaving the mode
    is :func:`~clio_agent.gact.spotter_watcher.sync_watcher_for_mode`'s). The new
    choice is checked first: a refused one puts the old value back and raises the
    typed 422 while the current watcher keeps watching. An accepted one disarms
    the current watcher and arms one on the new model.

    Raises:
        fastapi.HTTPException: 422 ``spotter_watcher_model_refused``.
    """

    from clio_agent.gact.spotter_arm_review import wake_on_arm  # noqa: PLC0415
    from clio_agent.gact.spotter_arming import refuse_watcher_arming  # noqa: PLC0415
    from clio_agent.gact.spotter_watcher import (  # noqa: PLC0415
        SPOTTER_APPROVAL_MODE,
        disarm_spotter_watcher,
        ensure_spotter_watcher,
    )

    if not prior_approval_mode == session.approval_mode == SPOTTER_APPROVAL_MODE:
        return
    if session_spotter_ref(session) == prior_spotter_model:
        return
    restore = {SPOTTER_MODEL_KEY: prior_spotter_model}
    refusal = model_refusal(app, session)
    if refusal is not None:
        refuse_watcher_arming(app, session, refusal, restore_metadata=restore)
    prior_ref = (
        prior_spotter_model.strip()
        if isinstance(prior_spotter_model, str) and prior_spotter_model.strip()
        else default_spotter_ref()
    )
    if prior_ref == watcher_model_ref(app, session):
        return  # The watcher already runs on what the new value resolves to.
    logger.info(
        "spotter_watcher_rearm session=%s model=%s prior=%s",
        session.id,
        watcher_model_ref(app, session) or "inherited",
        prior_ref or "inherited",
    )
    disarm_spotter_watcher(app, session)
    task = ensure_spotter_watcher(app, session)
    if task is not None:
        wake_on_arm(app, session, task)


__all__ = [
    "SPOTTER_MODEL_KEY",
    "default_spotter_ref",
    "is_person_choice",
    "model_refusal",
    "rearm_on_model_change",
    "watcher_model_ref",
]
