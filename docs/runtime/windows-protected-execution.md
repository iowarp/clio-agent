# Windows Desktop protected execution

Connected-source credentials must be excluded from child-process reads. CLIO
therefore blocks shell and MCP tools when Windows protected execution has not
been verified, including commands that only query a public website.

## Installation and recovery

The managed Codex Python distribution contains the main client. CLIO completes
it with `codex-command-runner.exe` and `codex-windows-sandbox-setup.exe` from the
same official Codex release. It selects the executable's PE architecture,
checks official release sizes and SHA-256 digests, and verifies cached files
before reuse. It never substitutes an older global helper or a latest release
that differs from the selected client. Managed client updates perform the same
check before being accepted.

Desktop runtime installation grants restricted sandbox users read and execute
access to the bundled runtime, excluding adjacent private Desktop data. If the
sandbox accounts already exist, installation verifies the fence for this CLIO
installation and records its own result without repeating UAC or account setup.
Failed verification cannot produce a successful installation receipt.

On a fresh machine, open **Infrastructure > Agent > Protected execution** and
select **Set up protected execution**. This explicit action performs the
one-time account setup with UAC and then verifies enforcement. For a headless
installation, use `clio sandbox setup`.

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
The verifier first requires an actual child write inside its allowed folder,
then requires a nonzero exit and an absent output file outside that folder.
A launcher failure or a missing output by itself never qualifies the fence.

Verification uses file witnesses and discards subprocess output. This avoids
waiting for pipe handles inherited by Codex's background ACL helpers while
keeping the existing 60-second verification limit.

## Recorded source qualification

The actual bundled Codex 0.157.1 was tested in an isolated owned directory with
matching official helpers. The native child wrote inside its allowed folder,
was denied outside it, queried GitHub's public CLIO Coder release API using
Python, and was denied a read of an isolated credential file. The installed
Desktop executable, runtime files, sign-ins, and installation receipt were not
replaced. This evidence establishes the source/native execution correction;
it is separate from installing and launching a newly packaged Desktop release.
