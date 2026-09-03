"""Credential redaction for MCP server specs (the error/log/wire-safe view).

An MCP declaration carries credentials in FOUR places, not two: ``headers`` and
``auth`` obviously, ``env`` by long-standing convention (``GITHUB_TOKEN`` and
friends), and -- least obviously -- ``command``/``args``/``url``, because
``${VAR}`` is expanded at spec construction, so a declaration like
``ndp-server --token ${NDP_TOKEN}`` is already holding the token by the time
anything redacts it. The rule for all four is the same: keep the NAME, drop the
value. For argv and url that means serving the pre-expansion declaration, which
is both non-secret and more useful than ``<redacted>`` -- it names the variable
an operator has to fix.

Owner module (#775 no-accretion): this concern grew past a one-function footnote
inside ``mcp_config``, which owns declaration parsing and transports.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["redact_mcp_spec"]


def redact_mcp_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return an error/log-safe MCP spec with all client credentials removed."""
    redacted = dict(spec)
    headers = redacted.get("headers")
    if isinstance(headers, Mapping):
        redacted["headers"] = {str(key): "<redacted>" for key in headers}
    if redacted.get("auth") is not None:
        redacted["auth"] = "<redacted>"
    # env values are the common credential carrier for stdio servers
    # (GITHUB_TOKEN, API keys); redact values, keep the variable names.
    env = redacted.get("env")
    if isinstance(env, Mapping):
        redacted["env"] = {str(key): "<redacted>" for key in env}
    # ...and argv/url are the SECOND carrier: `--token ${GITHUB_TOKEN}` is already
    # the token by the time a spec exists. Serve the declaration instead.
    declared = redacted.pop("declared", None)
    declared = declared if isinstance(declared, Mapping) else {}
    for key in ("command", "url"):
        text = declared.get(key)
        if isinstance(text, str) and text:
            redacted[key] = text
    redacted["args"] = _redacted_argv(redacted.get("args"), declared.get("args"))
    return redacted


def _redacted_argv(expanded: Any, declared: Any) -> list[str]:
    """Return argv as DECLARED, masking wholesale when expansion re-shaped it.

    An expanded value carrying whitespace re-splits the vector, so positions no
    longer align with the declaration and substituting element-wise would hand
    back the wrong token. A misaligned argv is masked rather than guessed at.
    """

    argv = [str(value) for value in expanded or ()]
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        return argv
    declared_argv = [str(value) for value in declared]
    if len(declared_argv) != len(argv):
        return ["<redacted>"] * len(argv)
    return declared_argv
