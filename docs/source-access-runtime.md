# Source access and context recovery

## User behavior

`connected_data_status` reports the workspace's primary and additional folders,
saved sources, and GitHub/Google Drive/Globus account status. Empty remote sources
do not establish that workspace files are missing. Its Activity presentation is
the inventory the agent received, without an embedded setup button.

An ordinary A2UI Button in the answer can use the returned
`data_source/login/{provider}` event. The server audits an owner-bound click as
`client_ui`; it does not start an agent turn or obtain credentials. The persistent
client host opens the existing private sign-in UI. It survives a surface moving
from the pending response into the transcript. Foreign CLIO/workspace contexts,
extra parameters, unknown providers and offline archives cannot perform sign-in.

Settings → Data sources reuses the account and source controls from Attach.
Accounts belong to the CLIO owner; sources belong to the selected workspace.
Switching workspaces resets source details without changing Attach's selection.
Accounts can be managed before any workspace is registered.

`connected_data_connect` validates a nonsecret source request before persisting
it. New connections pass through the existing native permission gate; bypass is
audited, and explicit deny/ask rules remain effective. Identical approved sources
are reused. Registration, sign-in and a completed linked index are separate
states. Linking produces source references, rather than an operating-system mount.
Existing read-only, working-copy review and write-enabled rules are preserved.

## GitHub through the normal shell

Ordinary GitHub work uses the full `gh` CLI through `shell_bash`, including
cloning, repository inspection, issues, pull requests, Actions and releases.
The shared editable `clio.runtime.github_shell` prompt is included at the common
GACT agent-loop request boundary whenever the agent declares a shell. It applies
to every provider and to root and child agents with that capability. It does not
attach a shell to agents without one. A repository URL does not request a source
connection. Public API/browser reads remain available when the CLI cannot read.

The shell keeps an existing `gh` on its PATH and its configured CLI account.
When none is available, it adds the already installed, verified managed CLI to
the child process's PATH. This lookup downloads nothing, mutates no global PATH
and injects no CLIO account grant. The ordinary shell permission gate and
filesystem boundary continue to apply. There is no `github_cli` agent tool or
restricted wrapper around ordinary shell commands.

Release guidance inspects live publication records, excludes drafts, and
distinguishes stable releases from prereleases. Local tags and the latest-stable
endpoint do not establish the newest published beta.

### Explicit connected-source validation

The normal document/runtime installation hook installs official `gh` 2.102.0
into CLIO's private cache. Version and archive SHA-256 values are pinned for
Linux, macOS and Windows on AMD64/ARM64. The executable is verified on reuse;
archive paths are not extracted wholesale. This does not add the CLI archives
to the Desktop installer payload.

For an explicitly requested source attachment, internal connection validation
resolves the current CLIO account privately and
enforces source ownership, workspace, repository, folder and revision. Inherited
host CLI authentication/debug variables are removed. CLIO supplies a private
process grant and a separate CLI config directory; it never runs `gh auth login`
or exposes tokens to the agent. Sign-out prevents later authenticated calls.

These checks belong to connected-source validation, not the agent's tool catalog.
Connected-source writes continue through editing/review/publication.
GitHub app permissions determine upstream repository access independently of
CLIO's source approval. Existing linked-source filesystem adapters are retained.

## Context recovery and the OPAL observation

The OPAL transcript records a Desktop restart in the 13:36 follow-up, after the
13:22 response. The old recovery condition was an empty ARC scope; elapsed time
was not its trigger. The historical trace contains prior context operations but
does not record the old process/configuration/namespace identity, so it cannot
prove why that particular scope disappeared or identify a Codex timeout.

The earlier durability migration already made newly managed core configurations
durable. This change also rejects an effectively adopted volatile daemon
configuration, covering the shared-daemon first-configuration behavior.

Each step now records the actual process, context mode, scope, store configuration,
namespace and presence of context. When context is absent, recovery reads the
durable local journal through the default provenance dispatcher and restores
recorded segments, identities, order, tombstones and provider continuation. A
later restore snapshot replaces the prior plane. Fan-out copies are not combined.
Bounded staging chunks and the final index publication avoid a partially visible
restoration. A populated concurrent scope is not overwritten.

If operations are unavailable, the fallback reconstructs stored transcript parts,
including recorded tool inputs/results, loaded skill text and injections. Its
receipt explicitly identifies missing provider continuation. Missing result text
is identified as a gap; unresolved tool calls reject restoration before seeding.
Corrupt journal records fail explicitly instead of silently dropping context.

The transcript receipt exposes the exact restored content through Show what it
got and Open exact details. This does not paraphrase commands or skill contents.

## Retained acceptance evidence

Local evidence and untouched screenshots are archived under:

`D:/Libraries/Videos/clio_recordings/2026-10-05-source-access-runtime/`

The browser acceptance used the default agent with Codex/Luna and an explicitly
synthetic three-row dataset. It verified workspace inspection, an answer-level
Google Drive button, the private dialog, same-store Agent restart, and controlled
context loss followed by a 44-segment durable restoration. The live context
and historical context API views read successfully afterward. This Windows core
deployment reports `clio_core_search_indexer_absent` for semantic search; that
capability is distinct from context restoration.

The real authenticated managed-CLI check used Desktop's existing GitHub account
via production StorageAuth and source_cli, without copying credentials or changing
the account. It read the approved develop/docs scope and rejected outside-folder
access. That specific authenticated check was a production API invocation, not a
new agent conversation. The fresh Settings frontend showed the same Desktop
GitHub and Globus sign-ins.

The initial acceptance retained two malformed filesystem calls and an unavailable
catalog selection, which the agent recovered from with shell reads and a later
surface creation. An earlier recovery take exposed the provenance reader wiring
gap; it was repaired and the durable-journal path was rerun. These are retained
in the evidence, rather than replaced with fabricated successful interactions.

The source changes are intended for develop. They do not change the frozen beta-3
release tag, rebuild the native Desktop executable, or publish a new release.
