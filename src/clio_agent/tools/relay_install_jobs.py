"""Job execution + in-memory registry for the relay-install surface (clio-relay#209 A2).

The execution half of ``tools/relay_cli_runner.py`` (config/parsing/typed errors --
split into this sibling module to hold each under the per-file size ratchet, #774):
:class:`RelayInstallJob` / :class:`RelayInstallJobRegistry` are the in-memory job
ledger, :func:`start_relay_install_job` spawns a long, SSH-dialing operation on a
background thread and returns a job handle immediately, and
:func:`run_bounded_relay_cli` runs a fast, non-dialing operation to completion inline.

No new persistent store (RULE 4): the registry is in-memory only, mirroring
``gact/agent_tasks.py``'s ``AgentTaskRegistry`` shape (dict + lock,
``dataclasses.replace`` under the lock) but WITHOUT session-backed durability, and it
retention-bounds itself (terminal-first eviction past a soft cap, force-eviction past
a hard cap) mirroring ``gact/runtime/retention.py``'s ``ledger_retention`` policy
shape -- reimplemented locally rather than imported, since ``tools/`` may not depend
on ``gact/`` (the reverse dependency direction is the one this codebase allows).

Two known, accepted-for-now scope limits (documented rather than fixed in this
slice):

* **Registry is per-``RelayInstallSurface`` instance, not process-global.** Each
  ``discover_relay_tool_surfaces()`` call (boot, and every TTL-triggered relay
  catalog refresh -- see ``gact/relay_wiring.py``) constructs a fresh
  ``RelayInstallSurface`` with a fresh, EMPTY job registry. A job started before a
  refresh becomes unreachable through the new surface after one (its subprocess
  keeps running; only the poll handle is orphaned). Job ids are ``uuid4``-random
  (unguessable), which is accepted as adequate for now given this gap -- a future
  slice that needs cross-refresh job continuity should promote the registry to a
  real process-global singleton.
* **A daemon-thread driver does NOT die with clio-agent, nor does its child
  subprocess.** Python daemon threads stop being SCHEDULED at interpreter exit --
  they get no chance to run cleanup, and the subprocess they Popen'd is a genuinely
  separate OS process with no job-object/process-group tether back to clio-agent's
  own lifecycle here. A clio-agent restart mid-bootstrap therefore ORPHANS the
  ``clio-relay`` subprocess, which keeps dialing independently. This is stated
  honestly rather than silently implied otherwise; a future slice wanting a hard
  guarantee would need an explicit shutdown-hook process-tree sweep (mirroring
  ``runtime/process_tree.py``'s boot-time reaper), not attempted here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import psutil

from clio_agent.tools.relay_cli_runner import (
    MAX_RETAINED_RECEIPT_FIELDS,
    STATE_FAILED,
    STATE_NEEDS_USER_ATTENTION,
    STATE_RUNNING,
    TERMINAL_STATES,
    RelayCliJobError,
    RelayCliReceiptField,
    _classify_exit_state,
    _clip,
    _detect_actionable_refusal,
    _parse_marker_line,
    _subprocess_env,
    job_retention_hard_cap,
    job_retention_max_entries,
    output_tail_bytes,
    parse_relay_cli_stdout,
    resolve_relay_cli_executable,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RelayInstallJob:
    """One local subprocess-backed relay-install operation.

    In-memory only (RULE 4: no new persistent store) -- lifecycle mutations go
    through :class:`RelayInstallJobRegistry`, which produces a new record via
    ``dataclasses.replace`` under its lock, mirroring ``gact/agent_tasks.py``'s
    ``AgentTask``/``AgentTaskRegistry`` shape without the session-backed durability.
    """

    job_id: str
    kind: str
    argv: tuple[str, ...]
    created_at: str
    updated_at: str
    last_output_at: str
    #: The cluster this job targets, when known (M8: the (cluster, kind) duplicate-
    #: run guard's lookup key). "" for a job with no single cluster (none currently).
    cluster: str = ""
    state: str = STATE_RUNNING
    receipt_fields: tuple[RelayCliReceiptField, ...] = ()
    #: F3: set when more markers arrived than :data:`~clio_agent.tools.relay_cli_runner.
    #: MAX_RETAINED_RECEIPT_FIELDS` retains -- the drop is never silent.
    receipt_fields_truncated: bool = False
    parsed_document: dict[str, Any] | None = None
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error_reason: str = ""
    actionable_refusal: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_wire(self) -> dict[str, Any]:
        """Return the advertised handle-first / terminal-result wire shape."""

        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "terminal": self.terminal,
            "exit_code": self.exit_code,
            "receipt_fields": [f.to_wire() for f in self.receipt_fields],
            "receipt_fields_truncated": self.receipt_fields_truncated,
            "parsed_document": self.parsed_document,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "error_reason": self.error_reason,
            "actionable_refusal": self.actionable_refusal,
        }


def effective_job_state(job: RelayInstallJob, *, idle_seconds: float) -> str:
    """Return ``job.state``, relabeled ``needs_user_attention`` when appropriate.

    A RUNNING job with no new output for ``idle_seconds`` is reported as needing the
    operator's attention (a suspected SSH/2FA prompt clio-relay has no
    non-interactive bound for) -- this is computed fresh at every read from
    wall-clock recency, never a stored flag that could go stale, and it never kills
    the process: the job stays pollable and may still complete normally.
    """

    if job.state != STATE_RUNNING:
        return job.state
    last = datetime.fromisoformat(job.last_output_at)
    if (datetime.now(timezone.utc) - last).total_seconds() > idle_seconds:
        return STATE_NEEDS_USER_ATTENTION
    return job.state


class RelayInstallJobRegistry:
    """In-memory ``{job_id: RelayInstallJob}`` projection (dict + lock).

    Mirrors ``gact/agent_tasks.py::AgentTaskRegistry``'s dict-plus-lock,
    ``dataclasses.replace``-under-lock shape -- deliberately without that module's
    session-backed persistence (RULE 4: this is ephemeral runtime state, not a fifth
    durable store). Receipt fields (F3) are held OUTSIDE the frozen ``RelayInstallJob``
    record, in a plain per-job ``list`` appended in O(1) amortized time and merged
    into a snapshot tuple only when a caller actually reads the job (``get``) --
    decoupling the hot per-line append path from the cold per-poll read path is what
    turns the old "rebuild the whole tuple on every append" O(n^2) shape (measured
    18s at 64k lines) into O(n) total. Retention (F4) evicts terminal-first past a
    soft cap, then force-evicts the oldest job overall past a hard cap, mirroring
    ``gact/runtime/retention.py``'s policy shape.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RelayInstallJob] = {}
        self._receipt_fields: dict[str, list[RelayCliReceiptField]] = {}
        self._receipt_truncated: dict[str, bool] = {}

    def register(self, job: RelayInstallJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._receipt_fields.setdefault(job.job_id, [])
            self._receipt_truncated.setdefault(job.job_id, False)
            self._enforce_retention_locked()

    def get(self, job_id: str) -> RelayInstallJob | None:
        with self._lock:
            return self._snapshot_locked(job_id)

    def _snapshot_locked(self, job_id: str) -> RelayInstallJob | None:
        current = self._jobs.get(job_id)
        if current is None:
            return None
        fields = self._receipt_fields.get(job_id, [])
        truncated = self._receipt_truncated.get(job_id, False)
        if not fields and not truncated:
            return current
        return replace(current, receipt_fields=tuple(fields), receipt_fields_truncated=truncated)

    def field_count(self, job_id: str) -> int:
        """O(1) live field count -- never materializes the snapshot tuple."""

        with self._lock:
            return len(self._receipt_fields.get(job_id, []))

    def find_running(self, *, cluster: str, kind: str) -> RelayInstallJob | None:
        """Return a live (non-terminal) job for ``(cluster, kind)``, if any (M8)."""

        with self._lock:
            for job in self._jobs.values():
                if job.cluster == cluster and job.kind == kind and job.state not in TERMINAL_STATES:
                    return self._snapshot_locked(job.job_id)
            return None

    def append_receipt_field(self, job_id: str, field_: RelayCliReceiptField) -> None:
        with self._lock:
            fields = self._receipt_fields.get(job_id)
            if fields is None:
                return
            if len(fields) >= MAX_RETAINED_RECEIPT_FIELDS:
                self._receipt_truncated[job_id] = True
            else:
                fields.append(replace(field_, seq=len(fields)))
            current = self._jobs.get(job_id)
            if current is not None:
                now = _now_iso()
                self._jobs[job_id] = replace(current, updated_at=now, last_output_at=now)

    def note_output(self, job_id: str, *, stdout_delta: str = "", stderr_delta: str = "") -> None:
        """Bump the liveness clock and fold a delta into the bounded raw tail(s).

        Both tails stay bounded at every step (``current.tail + delta`` then
        re-clip) so this is O(cap) per call, not O(total bytes seen) -- the same
        already-correct shape ``stderr_tail`` had before F3; stdout now gets the
        same treatment (previously accumulated unboundedly in the driver thread and
        was only clipped once at the very end).
        """

        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            now = _now_iso()
            updates: dict[str, Any] = {"updated_at": now, "last_output_at": now}
            if stdout_delta:
                updates["stdout_tail"] = _clip(
                    current.stdout_tail + stdout_delta, output_tail_bytes()
                )
            if stderr_delta:
                updates["stderr_tail"] = _clip(
                    current.stderr_tail + stderr_delta, output_tail_bytes()
                )
            self._jobs[job_id] = replace(current, **updates)

    def set_terminal(
        self,
        job_id: str,
        *,
        state: str,
        exit_code: int | None,
        error_reason: str = "",
        parsed_document: dict[str, Any] | None = None,
        actionable_refusal: dict[str, Any] | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
    ) -> None:
        if state not in TERMINAL_STATES:
            raise RelayCliJobError(f"unknown terminal state {state!r}", reason="unknown_state")
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            self._jobs[job_id] = replace(
                current,
                state=state,
                exit_code=exit_code,
                error_reason=error_reason,
                parsed_document=parsed_document
                if parsed_document is not None
                else current.parsed_document,
                actionable_refusal=actionable_refusal,
                stdout_tail=stdout_tail or current.stdout_tail,
                stderr_tail=stderr_tail or current.stderr_tail,
                updated_at=_now_iso(),
            )
            self._enforce_retention_locked()

    def _enforce_retention_locked(self) -> None:
        """F4: terminal-first eviction past the soft cap, then force past the hard
        cap. Caller MUST already hold ``self._lock`` (not reentrant)."""

        max_entries = job_retention_max_entries()
        hard_cap = job_retention_hard_cap()
        while len(self._jobs) > max_entries:
            victim = next((jid for jid, job in self._jobs.items() if job.terminal), None)
            if victim is None:
                break
            self._evict_locked(victim)
        while len(self._jobs) > hard_cap:
            victim = next(iter(self._jobs), None)
            if victim is None:
                break
            logger.warning(
                "relay install job registry hard cap exceeded; force-evicting "
                "job=%s (its subprocess, if still running, is now orphaned from "
                "this registry -- see the module docstring)",
                victim,
            )
            self._evict_locked(victim)

    def _evict_locked(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        self._receipt_fields.pop(job_id, None)
        self._receipt_truncated.pop(job_id, None)


def _kill_process_tree(proc: "subprocess.Popen[str]") -> None:
    """Kill ``proc`` and every descendant, never just the immediate child.

    On Windows the resolved executable may itself be a thin OS dispatcher (a
    ``.cmd`` wrapper) whose real work happens in a CHILD process it spawns;
    killing only ``proc`` leaves that grandchild running and holding the stdout
    pipe open, so the reader thread never observes EOF and the job hangs well
    past its timeout. Enumerated via psutil (an existing core dependency, same
    idiom as ``serve.py``'s own process-tree teardown) -- best-effort, never
    raises out of the driver thread.
    """

    children: list[Any] = []
    parent: Any = None
    with suppress(Exception):
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    for victim in children:
        with suppress(Exception):
            victim.kill()
    if parent is not None:
        with suppress(Exception):
            parent.kill()
    else:
        with suppress(Exception):
            proc.kill()


def _popen_kwargs() -> dict[str, Any]:
    """Windows: keep the console window hidden (parity with codex_app_server.py)."""

    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def start_relay_install_job(
    registry: RelayInstallJobRegistry,
    *,
    kind: str,
    cluster: str,
    argv: Sequence[str],
    executable: str,
    timeout_seconds: float,
) -> RelayInstallJob:
    """Spawn ``executable argv`` in a background thread; return the running handle now.

    The subprocess is driven to terminal on a daemon thread
    (:func:`_drive_relay_install_job`) that streams stdout/stderr line-by-line,
    folding each declared-marker receipt line into the registry as it arrives -- the
    caller never blocks on the operation itself, only on this synchronous spawn (a
    Popen call, not the SSH dial it starts). ``cluster`` is stamped on the record for
    the (cluster, kind) duplicate-run guard (M8); the subprocess environment is an
    explicit allowlist keyed by ``kind`` (F5b), never the full inherited environment.
    """

    job_id = f"relayjob_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    job = RelayInstallJob(
        job_id=job_id,
        kind=kind,
        argv=tuple(argv),
        created_at=now,
        updated_at=now,
        last_output_at=now,
        cluster=cluster,
        state=STATE_RUNNING,
    )
    registry.register(job)

    full_argv = [executable, *argv]
    try:
        proc = subprocess.Popen(
            full_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_subprocess_env(kind),
            **_popen_kwargs(),
        )
    except OSError as exc:
        registry.set_terminal(
            job_id,
            state=STATE_FAILED,
            exit_code=None,
            error_reason="relay_cli_spawn_failed",
            stderr_tail=_clip(str(exc), output_tail_bytes()),
        )
        resolved = registry.get(job_id)
        assert resolved is not None
        return resolved

    thread = threading.Thread(
        target=_drive_relay_install_job,
        args=(registry, job_id, kind, proc, timeout_seconds),
        name=f"clio-relay-install-{job_id}",
        daemon=True,
    )
    thread.start()
    return job


def _drive_relay_install_job(
    registry: RelayInstallJobRegistry,
    job_id: str,
    kind: str,
    proc: "subprocess.Popen[str]",
    timeout_seconds: float,
) -> None:
    """Background-thread driver: stream both pipes, enforce the runaway backstop.

    Two reader threads (never a single blocking ``communicate()``, which would
    withhold every line until the process exits and defeat incremental progress)
    fold stdout lines into typed receipt fields as they land and accumulate the
    bounded stdout/stderr tails (F3: both tails are now live/incremental, capped at
    every step -- neither an unbounded in-memory list nor a single end-of-run clip).
    ``timeout_seconds`` is a RUNAWAY BACKSTOP, not the operational clock (CLAUDE.md
    ⚑ #6) -- it exists so a truly wedged subprocess cannot pin a thread and a
    registry slot forever, not to bound a normal SSH dial.
    """

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            registry.note_output(job_id, stdout_delta=line)
            parsed = _parse_marker_line(line.rstrip("\n"))
            if parsed is not None:
                registry.append_receipt_field(job_id, parsed)
        with suppress(Exception):
            proc.stdout.close()

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, ""):
            registry.note_output(job_id, stderr_delta=line)
        with suppress(Exception):
            proc.stderr.close()

    t_out = threading.Thread(target=read_stdout, daemon=True, name=f"{job_id}-stdout")
    t_err = threading.Thread(target=read_stderr, daemon=True, name=f"{job_id}-stderr")
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while proc.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
            _kill_process_tree(proc)
            break
        time.sleep(0.2)

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    current = registry.get(job_id)
    stdout_tail = current.stdout_tail if current is not None else ""
    stderr_tail = current.stderr_tail if current is not None else ""

    if timed_out:
        logger.warning(
            "relay install job timed out reason=relay_cli_timeout job=%s kind=%s timeout_s=%s",
            job_id,
            kind,
            timeout_seconds,
        )
        registry.set_terminal(
            job_id,
            state=STATE_FAILED,
            exit_code=None,
            error_reason="relay_cli_timeout",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
        return

    try:
        exit_code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        exit_code = None

    # The whole-document-JSON branch (session/relay-host commands) only ever needs
    # to see a small document -- parsing off the ALREADY-BOUNDED tail (rather than
    # an unbounded full-text accumulation) is correct for every real verb and is
    # what keeps this path O(1) in memory regardless of a bootstrap stream's size.
    _, whole_doc = parse_relay_cli_stdout(stdout_tail)
    state = _classify_exit_state(kind, exit_code)
    actionable = _detect_actionable_refusal(stdout_tail, stderr_tail, kind=kind)
    error_reason = ""
    if state == STATE_FAILED:
        error_reason = actionable["reason"] if actionable else "relay_cli_nonzero_exit"
    registry.set_terminal(
        job_id,
        state=state,
        exit_code=exit_code,
        error_reason=error_reason,
        parsed_document=whole_doc,
        actionable_refusal=actionable,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


def run_bounded_relay_cli(
    argv: Sequence[str], *, kind: str, timeout_seconds: float, tail_bytes: int | None = None
) -> RelayInstallJob:
    """Run one relay CLI invocation to completion within ``timeout_seconds`` (BLOCKING).

    For fast, non-SSH-dialing operations (register, doctor, installation-info,
    proxy-status) -- mirrors ``JarvisJobs._bounded``'s "wait to terminal within
    budget" shape, but purely locally: no relay task backend is involved. Callers on
    the async tool-call path MUST run this via ``asyncio.to_thread`` (it blocks the
    calling thread for up to ``timeout_seconds``); it does not touch a
    :class:`RelayInstallJobRegistry` because the result is already terminal by the
    time it returns -- there is nothing left to poll.

    ``tail_bytes`` overrides the configured :func:`output_tail_bytes` for THIS call
    only. Used by ``relay_cluster_register``'s existence check (F2): the default
    tail is sized for a receipt/log excerpt, not for a ``cluster list`` enumeration
    that could plausibly exceed it in a large registry -- silently truncating the
    match away would be the exact overwrite bug this check exists to prevent.
    """

    cap = tail_bytes if tail_bytes is not None else output_tail_bytes()
    executable = resolve_relay_cli_executable()
    job_id = f"relayjob_{uuid.uuid4().hex[:16]}"
    now = _now_iso()
    full_argv = [executable, *argv]
    try:
        completed = subprocess.run(
            full_argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=_subprocess_env(kind),
            **_popen_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return RelayInstallJob(
            job_id=job_id,
            kind=kind,
            argv=tuple(argv),
            created_at=now,
            updated_at=now,
            last_output_at=now,
            state=STATE_FAILED,
            exit_code=None,
            error_reason="relay_cli_timeout",
            stdout_tail=_clip(stdout, cap),
            stderr_tail=_clip(stderr, cap),
        )
    except OSError as exc:
        # M5: defense in depth against a TOCTOU race (the configured path existed
        # at resolve_relay_cli_executable()'s check but vanished before spawn).
        return RelayInstallJob(
            job_id=job_id,
            kind=kind,
            argv=tuple(argv),
            created_at=now,
            updated_at=now,
            last_output_at=now,
            state=STATE_FAILED,
            exit_code=None,
            error_reason="relay_cli_spawn_failed",
            stderr_tail=_clip(str(exc), cap),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    fields, whole_doc = parse_relay_cli_stdout(stdout)
    state = _classify_exit_state(kind, completed.returncode)
    actionable = _detect_actionable_refusal(stdout, stderr, kind=kind)
    error_reason = ""
    if state == STATE_FAILED:
        error_reason = actionable["reason"] if actionable else "relay_cli_nonzero_exit"
    return RelayInstallJob(
        job_id=job_id,
        kind=kind,
        argv=tuple(argv),
        created_at=now,
        updated_at=now,
        last_output_at=now,
        state=state,
        exit_code=completed.returncode,
        receipt_fields=tuple(fields),
        parsed_document=whole_doc,
        error_reason=error_reason,
        actionable_refusal=actionable,
        stdout_tail=_clip(stdout, cap),
        stderr_tail=_clip(stderr, cap),
    )


__all__ = [
    "RelayInstallJob",
    "RelayInstallJobRegistry",
    "effective_job_state",
    "run_bounded_relay_cli",
    "start_relay_install_job",
]
