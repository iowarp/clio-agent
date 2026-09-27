"""CLI selection + quiet-environment constants for the ``claude_code`` SDK
transport (B11/B12/B15 of the Claude subscription-provider tuning pass).

Owner module (#775 no-accretion): kept out of :mod:`claude_code_options` /
:mod:`claude_code_sessions` (both at or near their file-size ratchet).

**B12 (which CLI).** ``cli_path`` is pinned on every connect so no session
depends on the SDK's own PATH search. WHICH binary is decided in ONE place,
:func:`clio_agent.providers.components.client_binary.claude_client`: the
user's installed ``claude`` when it is newer than the one the SDK wheel
bundles (or when the wheel bundles none), otherwise the bundled one. Claude
Code gates new models by CLI version, so a lagging bundled CLI would hide
models the user's own install already serves. The selection is cached and
dropped by an explicit provider check or a component update. ``None`` (typed,
logged) only when no CLI exists at all -- the SDK's own discovery then raises
its install instructions.

**B11 (quiet environment).** :data:`QUIET_ENV` is merged by the SDK with the
INHERITED process environment (``ClaudeAgentOptions.env`` documents "explicit
env always wins"; verified against the installed SDK's subprocess transport),
so passing just these two keys is sufficient -- CLIO does not need to also
copy the parent environment itself.

**B15 (large tool outputs).** ``max_buffer_size`` bounds a single stdout line
the CLI subprocess transport can buffer before raising. CLIO's bare-model
transport (B3: ``tools=[]``) never streams a tool result through this
channel, but a large assistant turn (a long code block, a big JSON payload in
a ``submit`` argument) is still one line of the CLI's ``stream-json`` output,
and the SDK's own default (1 MiB) is tight enough to have been observed
tripping on those. :data:`MAX_BUFFER_SIZE` raises it to a named, documented
constant rather than leaving the tight default in place.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.components.client_binary import (
    VERSION_PROBE_TIMEOUT_S,
    claude_client,
    reset_client_cache,
)

__all__ = [
    "CLI_VERSION_PROBE_TIMEOUT_S",
    "MAX_BUFFER_SIZE",
    "QUIET_ENV",
    "claude_code_runtime_info",
    "reset_runtime_cache_for_tests",
    "resolve_cli_path",
]

#: B11: skip telemetry, update checks, and other non-essential network calls at
#: CLI startup. Merged by the SDK with the inherited process environment --
#: these two keys are added, not a full environment replacement.
QUIET_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
}

#: B15: 16 MiB, comfortably above any observed CLIO turn (a submit-tool JSON
#: payload or a long assistant answer serialized as one stream-json line) while
#: still bounding a genuinely runaway line. The SDK's own default is 1 MiB.
MAX_BUFFER_SIZE = 16 * 1024 * 1024

#: Bound on one ``--version`` probe (shared with the client selection).
CLI_VERSION_PROBE_TIMEOUT_S = VERSION_PROBE_TIMEOUT_S


def resolve_cli_path() -> str | None:
    """Return the ``claude`` CLI every ``claude_code`` session runs (cached selection)."""
    return claude_client().path


def reset_runtime_cache_for_tests() -> None:
    """Drop the cached CLI selection (test isolation)."""
    reset_client_cache()


def claude_code_runtime_info() -> dict[str, Any]:
    """The selected CLI (path, version, installed vs bundled, typed reason) for status surfaces."""
    selection = claude_client()
    return {
        "cli_path": selection.path,
        "cli_version": selection.client.version if selection.client else "",
        **selection.to_wire(),
    }
