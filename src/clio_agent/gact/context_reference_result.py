"""The one stable row shape every reference repository returns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.gact.context_reference_domain import REFERENCE_PART_TYPE_BY_KIND

__all__ = ["reference_result"]


def reference_result(
    *,
    kind: str,
    ref_id: str,
    label: str,
    detail: str,
    media_type: str,
    revision: str,
    navigation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact stable result shape shared by every repository.

    ``part_type`` rides every row so a client knows, before it attaches anything,
    which message part this identity becomes -- a ``resource`` row becomes a
    ``resource_ref``, everything else a ``context_ref``. Without it the picker had
    to guess, and guessing ``context_ref`` for a resource was a 400.
    """

    return {
        "kind": kind,
        "id": ref_id,
        "label": label,
        "detail": detail,
        "media_type": media_type,
        "revision": revision,
        "part_type": REFERENCE_PART_TYPE_BY_KIND.get(kind, "context_ref"),
        "navigation": dict(navigation),
    }
