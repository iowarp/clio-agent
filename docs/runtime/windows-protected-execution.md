# Windows Desktop protected execution

Connected-source credentials must be excluded from child-process reads. CLIO
therefore blocks shell and MCP tools when Windows protected execution has not
been verified, including commands that only query a public website.

## Installation and recovery

CLIO bundles Codex 0.162.1 and prefers native Microsoft Execution Containers
(MXC) after a real policy check succeeds. MXC needs neither extra Windows
accounts nor administrator setup or recursive permission grants. An OS build
number alone does not establish support. CLIO requires allowed workspace writes
and source reads, denied outside/source writes and credential reads (including
descendants), and denied direct external network access before selecting it.

The result is reused only while the client file identity/version, machine and
Windows update match. Unsupported results are also cached; explicit **Set up
protected execution** rechecks native policy, including a previously failed check. Commands use strict MXC selection
and managed requirements. A command failure never triggers a backend switch.

When MXC is unavailable, the managed Codex Python distribution contains the main client. CLIO completes
it with `codex-command-runner.exe` and `codex-windows-sandbox-setup.exe` from the
same official Codex release. It selects the executable's PE architecture,
checks official release sizes and SHA-256 digests, and verifies cached files
before reuse. It never substitutes an older global helper or a latest release
that differs from the selected client. Managed client updates perform the same
check before being accepted.

Legacy Desktop runtime installation grants restricted sandbox users read and execute
access to the bundled runtime, excluding adjacent private Desktop data. If the
sandbox accounts already exist, installation verifies the fence for this CLIO
installation and records its own result without repeating UAC or account setup.
Failed verification cannot produce a successful installation receipt.
A successful legacy check from a different Codex version must also be repeated
with the current client before activation; account presence alone is insufficient.

On a fresh machine requiring the legacy fallback, the Windows installer shows **Creating protected execution
sandbox - Windows approval required** and requests the one-time UAC approval.
After approval it creates the restricted accounts, grants runtime access and
verifies enforcement. An existing verified setup is reused without another
prompt. Declined approval or failed verification is shown as an installation
error and never recorded as success. Normal application startup does not create
accounts or request elevation.

For recovery, open **Infrastructure > Agent > Protected execution** and select
**Set up protected execution**, or use `clio sandbox setup` on a headless host.
The Desktop installer requests setup with `--setup-protected-execution`; normal
startup omits that flag. In the legacy fallback, both accounts receive read/execute access in one
traversal; successful grants are reused while root identity and ACL match.

MXC explicitly enables Codex's managed network proxy and chains it to CLIO's
per-child recorder. Direct non-loopback sockets are denied. Native host loopback
remains accessible directly; observability therefore identifies this boundary as
`proxy-enforced-external`, rather than claiming that every local connection was
recorded. Remaining descendants terminate when the foreground process exits, so
servers must remain owned by their running foreground tool process.

Older Desktop releases that omit the helpers or use native drive anchors in
permission profiles need an updated runtime containing this correction before
the setup action can recover their fence. Signing in to GitHub or another model
provider does not supply the missing helpers or verification receipt.

Ordinary GitHub inspection and cloning use the full `gh` CLI through the normal
shell. The shared GitHub prompt does not request connected-source setup merely
because a repository URL was supplied. Existing host CLI/account configuration
takes precedence; an already verified managed CLI is a PATH fallback. This does
not change the shell permission gate or connected-source credential exclusions.

## Enforcement proof

Windows profiles use Codex's supported `:root` read token, explicit workspace
write roots, and explicit read/deny rules for protected connected-source paths.
The legacy verifier first requires an actual child write inside its allowed folder,
then requires a nonzero exit and an absent output file outside that folder.
A launcher failure or a missing output by itself never qualifies the fence.

Verification uses file witnesses and discards subprocess output. This avoids
waiting for pipe handles inherited by Codex's background ACL helpers while
keeping the existing 60-second verification limit.

## Recorded source qualification

On October 9, Codex 0.162.1 passed CLIO's actual MXC setup with the installed
Desktop interpreter and an isolated CLIO home: 1.295 seconds for initial proof,
0.008 seconds for reuse. The resolved backend selected MXC without legacy helper
preparation, account setup or grants, and its composed PowerShell command ran.
A separate real CLIO chokepoint check recorded a public HTTPS request while
direct external sockets failed with Windows access-denied error 10013. These
are sandbox-stage measurements, not total installation/startup timings.
CLIO's actual `wrap_confined` seam also ran against an isolated connected-source
ledger: approved data remained readable, source mutation and credential reads
were denied, and the original source bytes remained unchanged.

The Desktop binary/runtime currently installed on the development host was not
replaced. A newly packaged installer and unsupported-host UAC creation still
require their own acceptance; the existing accounts were preserved.

The actual bundled Codex 0.157.1 was tested in an isolated owned directory with
matching official helpers. The native child wrote inside its allowed folder,
was denied outside it, queried GitHub's public CLIO Coder release API using
Python, and was denied a read of an isolated credential file. The installed
Desktop executable, runtime files, sign-ins, and installation receipt were not
replaced. This evidence establishes the source/native execution correction;
it is separate from installing and launching a newly packaged Desktop release.
