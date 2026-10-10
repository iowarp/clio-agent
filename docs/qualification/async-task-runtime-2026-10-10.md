# Integrated asynchronous task runtime qualification

The task implementation is integrated in core develop `4314bbb0`, with GACT
`e7d7de52` and marketplace `db6d6b7`. This does not establish full live acceptance.
Earlier model receipts retain their original source and runtime identities.

The isolated Linux qualification environment uses Python 3.14.6 and Core 2.3.1.
Its host has glibc 2.31. The integrated lock's `orjson==3.11.3` Python 3.14 Linux
wheel requires glibc 2.34; locked wheel installation therefore failed before any
model or task was submitted. Updating only the locked orjson package to 3.13.0
allows the existing environment to install the exact lock, including the Claude
SDK extra and Codex CLI 0.162.1. No runtime limits or task semantics changed.

Preflight commands, the original and updated locks, package inventories and
installation logs are retained under `mcp-agent-tasks/integrated-4314-runtime-preflight-1`
in the release-recovery evidence directory. Production installation and host
authentication are outside this qualification run.

Remaining acceptance includes final-source actual-model mixed controls,
completion delivery and lifecycle races. Unit tests, an installed environment,
and CI are supporting evidence; none replaces these live gates. Historical
failures and unavailable or prohibited routes remain open until separately
resolved.
