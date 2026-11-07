"""Content identity of a resolution (:func:`resolution_digest`).

Two resolutions with the same plugins, digests and prompt have the same digest even
when their plugins live in different store locations.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = ["resolution_digest"]

_LOCATION_KEYS = frozenset({"root", "path"})


def _without_locations(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _without_locations(v) for k, v in value.items() if k not in _LOCATION_KEYS}
    if isinstance(value, list):
        return [_without_locations(v) for v in value]
    return value


def resolution_digest(payload: Mapping[str, Any]) -> str:
    """Content identity of a resolution: plugins, digests and prompt, not store paths."""

    blob = json.dumps(
        _without_locations(payload), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
