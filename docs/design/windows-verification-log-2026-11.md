# Windows verification log (ARC performance work, 2026-11)

These changes were made and measured in a Linux cloud session (branch `claude/tool-calling-long-horizon`). Windows CI runs only `tests/test_native_services`, so nothing below has been checked on Windows. Each item says what changed, why Windows may differ, and what to check on a Windows machine with the desktop app or `clio-agent serve`.

How to use it:

- Work through the items in order and record the result under each one.
- Run the suites with `uv run --frozen --no-sync python -B scripts/run_tests.py <paths> -q`.
- Where an item says "decide", report what you found before changing defaults.

## 1. Log handles stay closed per line on Windows (decide whether to keep them open)

- **Change:** `src/clio_agent/runtime/append_handles.py` (`AppendHandles`) keeps one open append handle per JSONL log. It is used by the stream audit (`runtime/stream_audit.py`) and the semantic trace writer (`gact/semantic_trace_file.py`). On Linux this removed about a tenth of a busy server's CPU.
- **Why Windows differs:** an open handle blocks deleting or renaming the file. So `keep_open` defaults to `os.name != "nt"`, and on Windows each line still opens and closes the file, as before. Windows gets none of the gain.
- **What to check, with `AppendHandles(keep_open=True)` forced:**
  - Delete a session in the desktop app. Its `*.semantic.jsonl` trace must be deleted, or left behind without an error.
  - Delete or reset a workspace and its state directories.
  - Run `scripts/eval/tool_calling` (it reads the stream audit by time window).
  - Run the test suite's tmp-dir cleanup with `CLIO_STREAM_AUDIT_LOG` set.
  - Rotate a log by renaming it while the server runs.
  - Grep for code that removes or replaces trace or audit files: `rmtree`, `unlink` and `os.replace` near `semantic_traces` or `stream_audit`.
- **Decide:** if every path works, or can close the handle first (an `AppendHandles.release(path)` before deleting), make `keep_open` default true everywhere. Otherwise keep it as is.
- **Tests:** `tests/test_runtime/test_append_handles.py`, `tests/test_runtime/test_stream_audit.py`.

## 2. The store's daemon liveness probe is now cached for 3 s

- **Change:** `ClioCoreStore` built its `LivenessGate` with a literal `ttl_s=0.0`, probing the daemon before every store op. It now uses `arc.clio_core.liveness_ttl_s` (default 3.0 s) (`src/clio_agent/arc/storage.py`).
- **Why Windows differs:** on Windows the probe reads the OS listener table (`arc/windows_tcp.loopback_listener_present`), not a TCP connect. The zero TTL guarded against a native access violation when the daemon dies (clio-core#722). With 3 s, a daemon that dies inside the window is noticed at the next probe, or by the per-RPC stall watch.
- **What to check:**
  - Start `clio-agent serve`, start a long tool-heavy turn, and kill the clio-core daemon process.
  - The server must not crash with an access violation. It must report the typed `clio_core_daemon_not_listening` / `ClioCoreRuntimeLostError` (server log and doctor) within a few seconds.
  - Repeat a few times. If it ever crashes, set `arc.clio_core.liveness_ttl_s: 0` in the Windows defaults and report it.
- **Tests:** `tests/test_arc/test_clio_core_liveness.py`, `tests/test_arc/test_rpc_liveness.py`.

## 3. Reads use clio-core's async size and get

- **Change:** `get`, `get_raw` and `exists` call `AsyncGetBlobSize(tag_id, name)` and `AsyncGetBlob(tag_id, name, size, offset)`. They run on the caller thread once the kind's tag id is known (`arc/clio_core_async_ops.py`, `arc/storage.py`).
- **Why Windows differs:** the Windows build of iowarp-core must expose the same binding signatures. A not-found size must also return code 1, as on Linux.
- **What to check:**
  - Run `tests/test_arc/test_blob_frame.py` and `tests/test_arc/test_storage_companion.py` against the real daemon.
  - Start the server and open an existing session's transcript.
  - Run `uv run --frozen --no-sync python -m scripts.bench.arc_bench --namespace bench-win --only primitives`, compare `get_hit_300b` with Linux (0.57 ms p50), and record the result.

## 4. Daemon threads: `arc.core_threads` (default 8), seeded configs upgraded

- **Change:** the generated `cte.yaml` takes `runtime: num_threads` from `arc.core_threads` (default 8, was a literal 4). An existing CLIO seed with 4 is rewritten once, in place, at the next server start (`arc/clio_core_threads.py`). The daemon reads the new value when it next starts.
- **Why Windows differs:** the default seed lives under the Windows data directory. The daemon's thread split and CPU use there are unmeasured, and the machine may have few cores.
- **What to check:**
  1. Note `num_threads` in the seeded `cte.yaml` (path from `clio doctor`).
  2. Start the server and confirm the value became 8 and the log has the "upgraded in place" warning.
  3. Restart the daemon, then run a tool-heavy task and watch the daemon's CPU.
  4. On Linux the idle daemon used about 60% of a core (busy-polling). Record the Windows idle and busy figures.

## 5. A busy daemon is polled for 0.1 s before its progress is sampled

- **Change:** `await_future` polls a pending RPC for `arc.liveness.sample_after_s` (default 0.1 s) before the first daemon progress sample.
- **Why Windows differs:** sampling uses the Windows process APIs (`arc/daemon_progress.py`, `arc/clio_core_daemon.py`), and its cost there is unmeasured. Stall detection should be unchanged.
- **What to check:**
  - Suspend the daemon process (Resource Monitor or `pssuspend`) during a turn.
  - The store must degrade typed (`clio_core_rpc_stalled`) within `arc.liveness.stall_after_s` plus 0.1 s, not hang.
- **Tests:** `tests/test_arc/test_clio_core_async_ops.py`, `tests/test_arc/test_rpc_async_stall_ordering.py`, `tests/test_arc/test_daemon_progress.py`.

## 6. Per-session scratch folder as TEMP and TMP

- **Change** (merged earlier from `claude/session-scratch-folder`): the shell and the `run_code` kernel get `TMPDIR`, `TEMP` and `TMP` set to `<paths.scratch_root>/<machine>/<session>`. That folder is added to the session's write roots (`runtime/session_scratch.py`, `runtime/execution_environment.py`).
- **Why Windows differs:**
  - Windows tools read `TEMP` and `TMP`, not `TMPDIR`.
  - The Windows write fence (`clio sandbox setup`) must allow the folder.
  - Path length limits apply.
- **What to check:**
  - In a session, ask the agent to write a temporary file from PowerShell (`$env:TEMP`) and from a Python `run_code` script (`tempfile.gettempdir()`). Both must land in the session's scratch folder, inside the fence.
  - Deleting the session removes the folder.
  - A server restart after `paths.scratch_retention_days` sweeps old ones.

## Not platform-specific (no Windows check needed)

- Append-only chunk records.
- References to highway events in `arc.op`.
- The read-only MCP fast path (`tools.write_ahead`).
- The single settled snapshot for read-only inline calls.
- The lazy cross-job workspace set.
- The invocation index for `own_execution_tasks`.

These are pure Python over the store API. The Windows test runs above cover them.
