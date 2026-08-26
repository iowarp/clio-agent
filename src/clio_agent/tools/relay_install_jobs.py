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

M7 (review round 2, the ledger-wipe bug class): the registry used to be
per-``RelayInstallSurface`` instance, so every TTL-triggered relay catalog refresh
(``discover_relay_tool_surfaces()`` -- boot, and every #1227 D2 TTL window; see
``gact/relay_wiring.py``) constructed a fresh ``RelayInstallSurface`` with a fresh,
EMPTY job registry, and an in-flight job started before the refresh became
unreachable through the new surface mid-poll (``relay_install_job_not_found`` for a
subprocess that was still running). Fixed via :func:`default_relay_install_job_registry`,
a process-wide lazily-constructed singleton: production surface construction
(``tools/relay_factory.py::_build_relay_install_surface``) now threads THIS SAME
registry through every rebuild, so a job survives a catalog refresh exactly like its
subprocess does. ``RelayInstallSurface``'s own constructor default is UNCHANGED (a
fresh registry per instance when no ``job_registry`` is injected) precisely so tests
constructing it directly keep the isolation they already rely on -- only the
production factory call site opts into the shared singleton explicitly.

One known, accepted-for-now scope limit (documented rather than fixed in this
slice):

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
    _is_unrecognized_framed_line,
    _parse_marker_line,
    _subprocess_env,
    job_retention_hard_cap,
    job_retention_max_entries,
    output_tail_bytes,
    parse_relay_cli_stdout,
    parsed_document_max_bytes,
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
    #: R4 (small fix): count of stdout lines that matched the ``key=value`` framed
    #: SHAPE but were not under a declared clio-relay marker namespace -- never
    #: their content (F5a still never promotes that), just a count, so schema
    #: drift against a newer clio-relay release is queryable rather than silently
    #: invisible. See :func:`~clio_agent.tools.relay_cli_runner.parse_relay_cli_stdout`
    #: and :func:`~clio_agent.tools.relay_cli_runner._is_unrecognized_framed_line`.
    unrecognized_marker_count: int = 0
    parsed_document: dict[str, Any] | None = None
    #: R3 (MUST FIX, review round 2): set when the async driver's bounded
    #: whole-document buffer (:func:`~clio_agent.tools.relay_cli_runner.
    #: parsed_document_max_bytes`) was exceeded before the process exited --
    #: ``parsed_document`` is then ``None`` even though the job may otherwise be
    #: COMPLETED, and this flag is the typed signal that the loss happened rather
    #: than the document simply being absent.
    parsed_document_truncated: bool = False
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
            "unrecognized_marker_count": self.unrecognized_marker_count,
            "parsed_document": self.parsed_document,
            "parsed_document_truncated": self.parsed_document_truncated,
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
        #: R4: per-job running count of unrecognized-namespace framed markers
        #: seen so far, accumulated the same incremental way as receipt fields
        #: (:meth:`note_unrecognized_marker`) and merged into the snapshot only
        #: at read time (:meth:`_snapshot_locked`).
        self._unrecognized_marker_counts: dict[str, int] = {}

    def register(self, job: RelayInstallJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._receipt_fields.setdefault(job.job_id, [])
            self._receipt_truncated.setdefault(job.job_id, False)
            self._unrecognized_marker_counts.setdefault(job.job_id, 0)
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
        unrecognized = self._unrecognized_marker_counts.get(job_id, 0)
        if not fields and not truncated and not unrecognized:
            return current
        return replace(
            current,
            receipt_fields=tuple(fields),
            receipt_fields_truncated=truncated,
            unrecognized_marker_count=unrecognized,
        )

    def field_count(self, job_id: str) -> int:
        """O(1) live field count -- never materializes the snapshot tuple."""

        with self._lock:
            return len(self._receipt_fields.get(job_id, []))

    def note_unrecognized_marker(self, job_id: str) -> None:
        """R4: bump the per-job unrecognized-framed-marker count by one.

        O(1), never touches the frozen ``RelayInstallJob`` record itself --
        mirrors :meth:`append_receipt_field`'s decoupled hot-path shape.
        """

        with self._lock:
            if job_id not in self._jobs:
                return
            self._unrecognized_marker_counts[job_id] = (
                self._unrecognized_marker_counts.get(job_id, 0) + 1
            )

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
        parsed_document_truncated: bool = False,
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
                parsed_document_truncated=parsed_document_truncated,
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
        self._unrecognized_marker_counts.pop(job_id, None)


_default_registry_lock = threading.Lock()
_default_registry: RelayInstallJobRegistry | None = None


def default_relay_install_job_registry() -> RelayInstallJobRegistry:
    """Return the process-wide job registry singleton (M7, review round 2).

    Lazily constructed on first call, then reused for the lifetime of the
    process -- this is what lets an in-flight job survive a #1227 D2
    TTL-triggered relay catalog refresh: ``tools/relay_factory.py::
    _build_relay_install_surface`` threads THIS SAME registry through every
    ``RelayInstallSurface`` it (re)builds, rather than each rebuild minting its
    own fresh, empty one (the ledger-wipe bug this function fixes). Double-checked
    locking guards the one-time construction; every call after the first is a
    single ``is None`` check with no lock contention.

    Deliberately NOT the constructor default on :class:`RelayInstallSurface`
    itself -- tests construct that class directly and rely on getting an
    ISOLATED fresh registry per instance; only the production factory call site
    opts into this shared singleton explicitly.
    """

    global _default_registry
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = RelayInstallJobRegistry()
    return _default_registry


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


class _BoundedTextAccumulator:
    """Accumulate streamed text up to a byte budget without per-line reclipping.

    R3 (MUST FIX, review round 2): the async driver needs a buffer large enough
    for whole-document JSON parsing (session/relay-host commands print ONE
    document at the very end of their output), independent of the small DISPLAY
    tail (``stdout_tail``, capped at :func:`~clio_agent.tools.relay_cli_runner.
    output_tail_bytes` -- typically 4096 bytes). Re-clipping a growing string on
    every line (the ``_clip(current + delta, cap)`` shape :meth:`RelayInstallJobRegistry.
    note_output` already uses for the SMALL display tail) is O(cap) work per line;
    at a cap sized for a real document (:func:`~clio_agent.tools.relay_cli_runner.
    parsed_document_max_bytes`, default 256KB) that would reintroduce the same
    O(n*cap) shape F3 already fixed for unbounded receipt-field growth. Instead:
    append each chunk to a list (O(1) amortized per call) and stop retaining
    further chunks -- setting :attr:`truncated` -- once the running byte total
    crosses ``cap``. The caller must never attempt to parse a truncated buffer as
    JSON (a partial document is not valid JSON and silently trying anyway is
    exactly the kind of quiet failure this fix exists to avoid); it must instead
    surface :attr:`truncated` as a typed ``parsed_document_truncated`` reason.
    """

    def __init__(self, cap: int) -> None:
        self._cap = max(int(cap), 0)
        self._chunks: list[str] = []
        self._len = 0
        self.truncated = False

    def add(self, text: str) -> None:
        if self.truncated or not text:
            return
        self._chunks.append(text)
        self._len += len(text.encode("utf-8", errors="replace"))
        if self._len > self._cap:
            self.truncated = True

    def get(self) -> str:
        return "".join(self._chunks)


def start_relay_install_job(
    registry: RelayInstallJobRegistry,
    *,
    kind: str,
    cluster: str,
    argv: Sequence[str],
    executable: str,
    timeout_seconds: float,
    extra_env_names: Sequence[str] = (),
) -> RelayInstallJob:
    """Spawn ``executable argv`` in a background thread; return the running handle now.

    The subprocess is driven to terminal on a daemon thread
    (:func:`_drive_relay_install_job`) that streams stdout/stderr line-by-line,
    folding each declared-marker receipt line into the registry as it arrives -- the
    caller never blocks on the operation itself, only on this synchronous spawn (a
    Popen call, not the SSH dial it starts). ``cluster`` is stamped on the record for
    the (cluster, kind) duplicate-run guard (M8); the subprocess environment is an
    explicit allowlist keyed by ``kind`` (F5b), never the full inherited environment.
    ``extra_env_names`` (R1) forwards additional CALLER-KNOWN env var names on top of
    the kind-based allowlist -- e.g. a cluster's non-default frp/stcp secret env
    names, which this layer cannot resolve on its own (see ``_subprocess_env``).
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
            env=_subprocess_env(kind, extra_env_names=extra_env_names),
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

    doc_accumulator = _BoundedTextAccumulator(parsed_document_max_bytes())

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            registry.note_output(job_id, stdout_delta=line)
            doc_accumulator.add(line)
            stripped = line.rstrip("\n")
            parsed = _parse_marker_line(stripped)
            if parsed is not None:
                registry.append_receipt_field(job_id, parsed)
            elif _is_unrecognized_framed_line(stripped):
                registry.note_unrecognized_marker(job_id)
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

    # R3 (MUST FIX): parse the whole-document-JSON branch (session/relay-host
    # commands) off the SEPARATE bounded accumulator, never off ``stdout_tail``
    # -- that display tail is clipped to output_tail_bytes() (typically 4096
    # bytes), so any real document bigger than that silently lost
    # parsed_document before this fix. If even the LARGER accumulator bound was
    # exceeded, parsing is skipped and the loss is surfaced via
    # parsed_document_truncated rather than attempted against a truncated,
    # invalid-JSON buffer.
    if doc_accumulator.truncated:
        whole_doc: dict[str, Any] | None = None
        parsed_document_truncated = True
        logger.warning(
            "relay install job stdout exceeded the parsed-document bound; "
            "parsed_document dropped reason=parsed_document_truncated job=%s kind=%s",
            job_id,
            kind,
        )
    else:
        _, whole_doc, _ = parse_relay_cli_stdout(doc_accumulator.get())
        parsed_document_truncated = False

    # R2 (CRITICAL): classification now requires the ACTUAL parsed document, not
    # just the exit code -- click's own UsageError also exits 2, and only a
    # successfully parsed handle-only receipt proves clio-relay reached its own
    # documented non-failure branch rather than failing argument parsing first.
    state, reason_override = _classify_exit_state(kind, exit_code, parsed_document=whole_doc)
    actionable = _detect_actionable_refusal(stdout_tail, stderr_tail, kind=kind)
    error_reason = ""
    if state == STATE_FAILED:
        error_reason = reason_override or (
            actionable["reason"] if actionable else "relay_cli_nonzero_exit"
        )
    registry.set_terminal(
        job_id,
        state=state,
        exit_code=exit_code,
        error_reason=error_reason,
        parsed_document=whole_doc,
        parsed_document_truncated=parsed_document_truncated,
        actionable_refusal=actionable,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )


def run_bounded_relay_cli(
    argv: Sequence[str],
    *,
    kind: str,
    timeout_seconds: float,
    tail_bytes: int | None = None,
    extra_env_names: Sequence[str] = (),
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
    ``extra_env_names`` (R1) is the same caller-known-cluster escape hatch
    :func:`start_relay_install_job` accepts.

    R3 note: this path is unaffected by the async driver's tail-clipping bug --
    ``parse_relay_cli_stdout`` below runs against ``completed.stdout``, the FULL
    captured output ``subprocess.run(capture_output=True)`` already buffers, not
    against the clipped display tail computed further down.
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
            env=_subprocess_env(kind, extra_env_names=extra_env_names),
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
    fields, whole_doc, unrecognized_marker_count = parse_relay_cli_stdout(stdout)
    state, reason_override = _classify_exit_state(
        kind, completed.returncode, parsed_document=whole_doc
    )
    actionable = _detect_actionable_refusal(stdout, stderr, kind=kind)
    error_reason = ""
    if state == STATE_FAILED:
        error_reason = reason_override or (
            actionable["reason"] if actionable else "relay_cli_nonzero_exit"
        )
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
        unrecognized_marker_count=unrecognized_marker_count,
        parsed_document=whole_doc,
        error_reason=error_reason,
        actionable_refusal=actionable,
        stdout_tail=_clip(stdout, cap),
        stderr_tail=_clip(stderr, cap),
    )


__all__ = [
    "RelayInstallJob",
    "RelayInstallJobRegistry",
    "default_relay_install_job_registry",
    "effective_job_state",
    "run_bounded_relay_cli",
    "start_relay_install_job",
]
