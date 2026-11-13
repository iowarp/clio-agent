"""A view's own answer to "how do I reopen this?" (MA19, PV11).

CLIO saves the call that opened a view and replays it to reopen the view. The
call is not always the right thing to replay: ``open_view(path, new=true)``
starts an empty scene, so replaying it later wipes everything the person built
since. A server that knows better declares, in the private ``_meta`` of the
result that opens the view::

    _meta["ai.iowarp.clio/reopen"] = {"arguments": {"scene": "wavelet-3a3484d6"}}

CLIO then saves those arguments, not the call's, for the replay. The declaration
is taken off the result at admission, like the service declaration, so neither
the view nor the model sees it. A malformed declaration is ignored with a typed
reason (``reopen_declaration_invalid``), and the call's own arguments are saved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["REOPEN_META_KEY", "split_reopen_declaration"]

#: The private ``_meta`` key a server declares its reopen arguments under.
REOPEN_META_KEY = "ai.iowarp.clio/reopen"


def split_reopen_declaration(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """``(result without the declaration, declared arguments or None, reason)``.

    ``reason`` is empty when there is no declaration or it is valid, and
    ``reopen_declaration_invalid`` when one was present but unusable.
    """
    for key in ("_meta", "meta"):
        meta = result.get(key)
        if not isinstance(meta, Mapping) or REOPEN_META_KEY not in meta:
            continue
        declared = meta[REOPEN_META_KEY]
        stripped = dict(result)
        rest = {name: value for name, value in meta.items() if name != REOPEN_META_KEY}
        if rest:
            stripped[key] = rest
        else:
            stripped.pop(key, None)
        arguments = declared.get("arguments") if isinstance(declared, Mapping) else None
        if not isinstance(arguments, Mapping):
            return stripped, None, "reopen_declaration_invalid"
        return stripped, dict(arguments), ""
    return dict(result), None, ""
