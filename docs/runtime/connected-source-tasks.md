# Connected-source operations and MCP Tasks

Large folder indexing and downloads are candidates for the MCP Tasks extension.
The client supports that protocol today, but connected sources do not yet expose
their operations as MCP tasks. Keep that distinction visible when testing or
reporting progress.

## Current ownership and workload

| Operation | Work and limits | Current execution |
| --- | --- | --- |
| Connect/link a folder | Recursively list names, sizes and revisions, up to 100,000 entries. Linked files are read on demand. | Native `connected_data_connect` waits for indexing; the REST link route offloads I/O but still waits for completion. Neither returns an MCP task. |
| Download/refresh | Enumerate and verify the selection, stream and hash actual bytes in 1 MiB chunks, recheck the revision, publish a complete snapshot. | REST returns HTTP 202 with a durable `TransferOperation`; the storage owner runs the work in the background. |
| MCP tool returning a task | Persist its backend/session/task identity, poll using the server's interval, answer human-input requests, return the terminal result. | The ordinary tool call transparently awaits completion. Other client requests remain usable while it waits. |

Downloaded read-only data needs capacity for one immutable baseline. Editable
downloads also create a workspace working copy: the owner checks capacity for
two copies and separately checks workspace capacity. Refresh retains previous
copies, so retained history also consumes disk. These are implementation limits
and accounting rules, not measurements of a user's private sources.

Cancellation of a download is a durable request. The owner observes it between
chunks/file operations, cleans its staging tree, then records terminal state.
An acknowledgement must not imply that an in-flight read has already stopped.
Provider-owned transfers retain their submission ID; interrupted observation
can resume against the same job instead of submitting a duplicate.

## Qualification

Run focused cases individually, sequentially, with one worker. The real
Streamable HTTP server in `test_mcp_tasks_conformance.py` covers result delivery,
human input, reconnecting by task ID, task routing headers and cancellation
acknowledgement. `test_mcp_task_async_lifecycle.py` additionally proves that an
unrelated request completes while a task is working, and cancelling the normal
foreground wait sends one cancellation to the actual backend.

`test_source_transfer_async_lifecycle.py` exercises the real API, SQLite ledger
and filesystem transfer. A test-only reader pauses the second chunk of an actual
2,097,169-byte file; listing, hashing, publishing and cleanup use production code.
The tests verify HTTP 202, responsive health/control requests, durable byte
progress, an exact hash-matched final copy and acknowledged cancellation followed
by actual settlement without publishing a partial snapshot. This exposed a
Windows `WinError 206` when staging directories exceeded the ordinary path limit;
directory creation now uses the existing extended-path helper.

The tests also pause the real staging cleanup. The operation remains running
and control requests stay responsive until that cleanup finishes, for both
completion and cancellation. A deterministic pre-fix cancellation run reproduced
the premature terminal-state race observed under CI load.

Additional focused cases cover honoring `pollIntervalMs`, persisting before the
first poll, publishing wait events from a real app boot and loading task records
after process-state loss. This qualification does not measure large-folder
throughput or authenticate live Drive, GitHub, SFTP or Globus jobs.

## Integration contract

Expose bulk indexing and transfer through the official Tasks receiver while
retaining the existing storage owner and authorization rules. Do not introduce a
second downloader or label a private queue as an MCP task.

1. Create and persist the operation/task mapping before acknowledging the call.
   Scope it to the source, workspace, principal and backend session.
2. Report indexing, copying, verifying and publishing phases. Include discovered
   file counts and bytes; unknown totals stay unknown until enumeration finishes.
3. Project the owner's actual terminal result and business errors through the
   task result. Keep `input_required` on the existing approval/sign-in surface.
4. Forward cancellation to the owner, retain acknowledgement versus settlement,
   and keep prior snapshots intact. Stop and submitted-feedback handling must
   remain responsive while waiting.
5. Reconnect using the persisted operation and provider job IDs. Test process
   loss, duplicate observation, permission changes, interrupted transfers and
   source revision changes before enabling the mapping.

Keep quick status, small listings and sign-in discovery as ordinary calls. A
task lets the system wait reliably; releasing an agent to perform unrelated work
requires an explicit background/wait policy, since today's client transparently
awaits the terminal result.

Protocol reference: [MCP Tasks extension, 2026-07-28](https://modelcontextprotocol.github.io/ext-tasks/specification/2026-07-28/tasks.html).
