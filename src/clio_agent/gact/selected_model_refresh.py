"""Refresh discovery for a pinned model the active LM does not serve, before accepting.

A session (or one message) pinned to a provider/model other than the active
global LM runs on that pin only when the provider catalog holds discovery
evidence for that exact model (``message_submission`` gate). The catalog is a
snapshot: a managed deployment redeployed with another model, or a saved
server whose model list changed since the last probe, leaves it stale, and the
pinned session was refused with a typed 501 although the endpoint serves the
model (qualification c03). The only way out was a global rebind -- which
defeats pinning.

:func:`refresh_for_selected_model` re-discovers just the pinned provider (the
same ``read_catalog(refresh=True)`` the catalog route runs) when, and only
when, the pin has no evidence yet. The gate itself is unchanged: a model the
endpoint really does not serve is still refused, now against fresh evidence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.message_contract import PostMessageRequest

logger = logging.getLogger(__name__)

__all__ = ["refresh_for_selected_model"]


async def refresh_for_selected_model(app: "FastAPI", sid: str, req: "PostMessageRequest") -> None:
    """Re-discover the pinned provider when its model has no discovery evidence. Never raises."""

    from clio_agent.gact.modality_evidence import (  # noqa: PLC0415
        EVIDENCED_MODALITY_SOURCES,
        live_model_modalities,
    )
    from clio_agent.gact.provider_catalog_snapshot import read_catalog  # noqa: PLC0415
    from clio_agent.gact.providers.config import _model_ref_matches_active  # noqa: PLC0415
    from clio_agent.gact.session_host_agent import selected_ref  # noqa: PLC0415

    ref = selected_ref(app, sid, req)
    if ref is None or _model_ref_matches_active(ref, app):
        return
    if live_model_modalities(app, ref).evidence in EVIDENCED_MODALITY_SOURCES:
        return
    try:
        await read_catalog(app, refresh=True, provider_id=ref.provider_id)
    except Exception as exc:  # noqa: BLE001 - the gate then refuses with its typed 501
        logger.warning(
            "selected model discovery refresh failed reason=selected_model_refresh_failed "
            "session=%s provider=%s model=%s error=%r",
            sid,
            ref.provider_id,
            ref.model_id,
            exc,
        )
        return
    logger.info(
        "selected model discovery refreshed reason=selected_model_without_evidence "
        "session=%s provider=%s model=%s evidenced=%s",
        sid,
        ref.provider_id,
        ref.model_id,
        live_model_modalities(app, ref).evidence in EVIDENCED_MODALITY_SOURCES,
    )
