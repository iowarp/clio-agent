# Maintaining the Claude Code catalog

`claude-code-models.json` is the online source for Claude Code model identity
and documented input capabilities. CLIO fetches it from this repository's
`main` branch at discovery and explicit model refresh. Catalog changes can
therefore reach installed clients without waiting for a CLIO package release.
The cache preserves the last validated document when fetching fails.

Add new models as Anthropic announces them. Record the official
`minimum_client_version` when one is documented, rather than waiting for
the SDK's bundled CLI to catch up. Discovery compares that requirement with
the same installed-or-bundled binary the transport actually uses. Models
waiting for a client update are returned in the refresh result's informational
`rejected` list; other models remain usable. Refresh rechecks compatibility
after updating the managed client. No model turns are used to guess access.

Keep supported earlier models and the existing catalog default unless a
separate default change is intended. Do not infer account entitlements from
the catalog. Codex remains separate: its account model list comes directly
from the live service and is gated by the actual installed Codex client version.

As checked on 2026-10-08, [Anthropic's model configuration documentation](https://code.claude.com/docs/en/model-config)
requires Claude Code 2.1.280 for Opus 5.5, 2.1.284 for Sonnet 5.5 and 2.1.293
for Haiku 5.5. The [latest SDK release, 0.2.164](https://github.com/anthropics/claude-agent-sdk-python/releases/tag/v0.2.164)
still bundles 2.1.292. A catalog entry and a compatible runnable client are
distinct facts. Native Claude Code can also be updated with `claude update`;
an explicit provider check picks up a newer installed binary.

Validate changes against `claude-code-models.schema.json` and the real catalog
parser tests. Minimum versions must be numeric three-part versions. Focused
compatibility tests cover the gap, a subsequent client update, unknown client
version evidence and malformed maintenance edits.
