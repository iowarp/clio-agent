# Infrastructure host operations

Host inspection is the source of OS, architecture, accelerators, container availability,
hostname and home-directory facts. A connected SSH transport does not establish that
every service on that host is healthy. Allocation and scheduler preparation remain the
operator's responsibility; CLIO inspects the prepared execution host.

## Storage and terminal output

Structured host replies remove actual CSI/OSC terminal decoration before parsing.
This prevents SSH cursor-control output from becoming part of a storage root. JSON
escaped path content is preserved. Unexpected remaining control characters produce a
short actionable error before validating the individual storage locations.

Windows extended path prefixes remain available to filesystem operations. The client
displays ordinary drive/UNC paths. Desktop's managed local connection uses its native
directory dialog; remote hosts and tunneled connections use the selected server's
folder browser.

## Managed model downloads

Linux and Windows execution hosts support durable model acquisitions. The target
needs `uv`; local control uses the running CLIO Python executable, and remote control
uses the platform's Python. Downloading files does not start a model or require a
container runtime. Immutable revisions, free-space checks, hashes, partial-cache reuse
and the existing root registry remain authoritative.

Windows uses a byte-range control lock and queries process creation identity before
reusing an owner. Cancellation writes the existing durable marker; the verified worker
observes it and settles the cancelled receipt, preserving cached bytes. It never kills
an unrelated PID tree. Linux retains its process-group cancellation behavior.

## Provider and protected-execution health

Configuration source strings such as `env:CLIO_LM_MODEL` describe where the default
provider configuration came from. They do not establish an authentication failure.
Codex credentials alone are not a verified connection. A fresh successful live handshake
clears the warning; static model metadata cannot do so. The health projection reads the
existing handshake cache without making another network request. Provider setup exposes
the model refresh that performs that check.

Protected-execution setup returns its actual verdict to the client, which displays it
and refreshes the dedicated sandbox status and general health projection. Elevation
consent alone is not enforcement proof; the existing native conformance checks remain
required.

## Qualification

CI's installation-time helper provisioning can receive an explicit scoped
`CLIO_CODEX_RELEASE_TOKEN` from the workflow's read-only token, avoiding the anonymous
GitHub API limit on shared runners. Authorization applies only to the fixed release
metadata endpoint: metadata redirects fail, asset downloads receive no token, and the
receipt stores no token. Version, architecture, size and hash validation remain required.
No unrelated `GH_TOKEN`, provider account or global sign-in is reused.

The Windows process-query function also guards its own platform-specific body, so Linux
type checks do not resolve an unavailable Windows API. Four additional focused cases
cover metadata authorization/redirect containment, explicit installer token forwarding
and the real Windows process/lock path. Linux-targeted Mypy and scoped Pyright pass.

Nine focused backend cases pass individually with one worker. They cover decorated SSH
paths, invalid-path diagnostics, actual Windows locking/process identity with a test-only
launch seam, cancellation checkpoints, platform-aware API commands, handshake health,
hash verification, PID reuse and recorded-root retry/cancel behavior. This qualification
does not claim a fresh native UAC setup, live Ares model download or provider inference.
