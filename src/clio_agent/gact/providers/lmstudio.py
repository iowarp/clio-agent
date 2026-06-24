"""LM Studio native-REST helpers for the GACT server (#714 decomposition).

LM Studio exposes an OpenAI-compatible ``/v1`` surface for inference *and* a
native REST surface (``/api/v1/...``) for model lifecycle (load/unload). This
module owns the small set of helpers the provider-bind concern uses to talk to
that native surface:

* :func:`_lm_studio_api_root` -- derive the native REST root from an
  OpenAI-compatible base URL.
* :func:`_lm_studio_headers` -- build auth headers for native REST calls.
* :func:`_release_owned_lm_studio_instance` -- unload a CLIO-*owned* instance
  (never a user/GUI-loaded one) before a provider swap or shutdown.

Ownership invariant: CLIO records ``app.state.lm_studio_owned_instance`` ONLY
when it successfully loads a model via the native endpoint and receives an
``instance_id``; reused user-loaded instances are never marked owned and so are
never unloaded here. Imports stay lazy/leaf so this module never loads
``gact.app``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def _lm_studio_api_root(api_base: str) -> str:
    """Return the LM Studio native REST root for an OpenAI-compatible base URL."""

    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415

    parts = urlsplit(api_base.rstrip("/"))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _lm_studio_headers() -> dict[str, str]:
    """Build headers for LM Studio native REST calls."""

    headers = {"Content-Type": "application/json"}
    token = (
        os.environ.get("LM_STUDIO_API_TOKEN", "").strip()
        or os.environ.get("LM_API_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _release_owned_lm_studio_instance(
    app: "FastAPI",
    *,
    skip_instance_id: str = "",
    raise_on_error: bool = True,
) -> bool:
    """Unload a CLIO-owned LM Studio instance, never a user-owned one.

    CLIO records ownership only when it successfully calls LM Studio's
    native load endpoint and receives an ``instance_id``. Existing
    GUI/user-loaded instances are reused but never marked owned.
    """

    owned = getattr(app.state, "lm_studio_owned_instance", None)
    if not isinstance(owned, dict):
        return False

    instance_id = str(owned.get("instance_id") or "").strip()
    root = str(owned.get("root") or "").strip()
    if not instance_id or not root or (skip_instance_id and instance_id == skip_instance_id):
        return False

    try:
        import requests  # noqa: PLC0415

        response = requests.post(
            f"{root}/api/v1/models/unload",
            headers=_lm_studio_headers(),
            json={"instance_id": instance_id},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                "LM Studio model unload failed "
                f"({response.status_code}): {(response.text or '')[:300]}"
            )
    except Exception:
        # ``raise_on_error=False`` is an explicit opt-out used on the
        # best-effort shutdown / provider-swap paths: the caller has decided a
        # failed unload must not abort the swap (the old instance simply lingers
        # until LM Studio reclaims it). Re-raise only when the caller wants the
        # error surfaced; otherwise report the no-op so the caller can react.
        if raise_on_error:
            raise
        return False

    app.state.lm_studio_owned_instance = None
    return True
