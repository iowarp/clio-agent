"""Local subprocess runner for the DEPLOYED ``clio-relay`` CLI (install surface).

clio-relay#209 A2: expose relay cluster lifecycle operations (register, bootstrap,
status, session, proxy) as clio-agent-callable operations with typed progress. This
module is the execution seam: it resolves the ``clio-relay`` executable, runs it as a
LOCAL subprocess (never relay's own MCP/HTTP transport -- these operations manage the
cluster itself, before/without any relay door being reachable), parses its stdout into
typed fields, and drives long operations (bootstrap, an SSH-dialing session
start/attach, proxy install/teardown) on a background thread so the calling tool
returns a job handle immediately instead of blocking for minutes.

Two clio-relay stdout wire shapes exist (verified against the clio-relay source, not
guessed): ``cluster bootstrap`` prints one ``marker=json`` framed line per event
(``bootstrap_target_identity_pinned=...``, ``bootstrap_receipt_json=...``, ...);
``session``/``relay-host`` commands print ONE pretty JSON document
(``model_dump_json``). :func:`parse_relay_cli_stdout` handles both without an
allowlist -- unknown marker keys pass through verbatim, and no key/value is ever
decided on by keyword-matching prose (CLAUDE.md superseding principle #1): every
field emitted here is a structural parse of the CLI's OWN declared output, the same
class of parsing ``tools/execution.py``'s ``_structured_tool_result_error`` already
does for local tool stderr.

No new persistent store (RULE 4): :class:`RelayInstallJobRegistry` is in-memory only,
mirroring ``gact/agent_tasks.py``'s ``AgentTaskRegistry`` shape (dict + lock,
``dataclasses.replace`` under the lock) but WITHOUT session-backed durability -- an
in-flight job does not survive a clio-agent restart, exactly like the subprocess
itself would not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
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

from clio_agent import conf

logger = logging.getLogger(__name__)

#: The clio-relay console-script name (``[project.scripts] clio-relay = ...``).
RELAY_CLI_EXECUTABLE_NAME = "clio-relay"

STATE_RUNNING = "running"
STATE_NEEDS_USER_ATTENTION = "needs_user_attention"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED})

#: Exact stderr substring clio-relay's one-pass bash scripts print when the
#: systemd user-lingering gate refuses a persistent frpc proxy install (exit 78 on
#: the remote script; clio-relay's own CLI wraps this into a bare RelayError -> its
#: own process exit 1, so the substring -- not an exit code -- is the honest signal
#: available to a subprocess caller). Verified against clio-relay's
#: frpc_proxy_scripts.py; never a keyword guess on model prose (⚑ #1) -- this is a
#: structural parse of the CLI's OWN fixed error text, the same class of stderr
#: classification tools/execution.py's _is_transient_tool_error already does.
_LINGERING_GATE_SIGNATURE = "requires systemd user lingering"

_FRAMED_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)=(.*)$")


class RelayCliUnavailableError(RuntimeError):
    """The ``clio-relay`` executable could not be resolved."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = "relay_cli_unavailable"
        self.details = dict(details or {})


class RelayCliJobError(RuntimeError):
    """A rejected relay-install-surface operation (bad job id, bad arguments, ...)."""

    def __init__(self, message: str, *, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})


def resolve_relay_cli_executable() -> str:
    """Resolve the deployed ``clio-relay`` CLI, config -> env -> PATH.

    Deployment parameters are never hardcoded (house rule): ``relay.install_surface.
    cli_path`` / ``CLIO_RELAY_CLI_PATH`` overrides discovery for a non-standard
    install location; otherwise the console-script is looked up on ``PATH`` (a
    standard ``pip``/``uv tool install`` puts it there under its own name -- unlike
    the codex/claude CLIs, clio-relay is a plain Python console_script with no npm
    ``.cmd`` shim quirk to work around).
    """

    configured = conf.resolve(
        "relay.install_surface.cli_path",
        env="CLIO_RELAY_CLI_PATH",
        default="",
        cast=conf.as_str,
    ).strip()
    if configured:
        return configured
    found = shutil.which(RELAY_CLI_EXECUTABLE_NAME)
    if not found:
        raise RelayCliUnavailableError(
            f"{RELAY_CLI_EXECUTABLE_NAME!r} was not found on PATH and "
            "relay.install_surface.cli_path is unset",
            details={"executable": RELAY_CLI_EXECUTABLE_NAME},
        )
    return found


def _resolve_seconds(key: str, env: str, default: float) -> float:
    """Resolve one install-surface timing knob, config -> env -> default."""

    return float(
        conf.resolve(f"relay.install_surface.{key}", env=env, default=default, cast=conf.as_float)
    )


def long_operation_timeout_seconds() -> float:
    """Runaway backstop shared by every SSH-dialing long operation this surface
    drives asynchronously (``cluster bootstrap``, ``session start``/``attach``/
    ``teardown``, ``relay-host install-proxy``/``teardown-proxy``) -- a ceiling, not
    the operational clock (CLAUDE.md ⚑ #6): a normal dial finishes in seconds to a
    few minutes, this exists only to reclaim a truly wedged subprocess."""

    return _resolve_seconds(
        "long_operation_timeout_seconds", "CLIO_RELAY_INSTALL_LONG_OP_TIMEOUT_S", 900.0
    )


def bounded_timeout_seconds() -> float:
    """Timeout for a fast, non-SSH-dialing sub-probe (register, doctor, status)."""

    return _resolve_seconds("bounded_timeout_seconds", "CLIO_RELAY_INSTALL_BOUNDED_TIMEOUT_S", 60.0)


def attention_idle_seconds() -> float:
    """No-output duration after which a still-running job is labeled needing the
    operator's attention (an SSH/2FA prompt the CLI has no non-interactive bound
    for -- see ``docs/connection-model.md``'s "user present at bring-up" doctrine).
    This RELABELS an in-flight job's reported state; it never kills the process."""

    return _resolve_seconds("attention_idle_seconds", "CLIO_RELAY_INSTALL_ATTENTION_IDLE_S", 45.0)


def output_tail_bytes() -> int:
    """Bound on the retained stdout/stderr tail (never an unbounded buffer)."""

    return int(
        _resolve_seconds("output_tail_bytes", "CLIO_RELAY_INSTALL_OUTPUT_TAIL_BYTES", 4096.0)
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, max_bytes: int) -> str:
    """Clip text to its trailing ``max_bytes`` (the tail is the actionable part)."""

    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    return raw[-max_bytes:].decode("utf-8", errors="replace")


def _detect_actionable_refusal(stdout: str, stderr: str) -> dict[str, Any] | None:
    """Classify a known, structural clio-relay refusal into a typed remediation.

    Currently the one case: the systemd user-lingering gate a persistent frpc proxy
    install/teardown refuses on. Returns ``None`` for every other failure -- those
    stay a bare ``relay_cli_nonzero_exit`` with the bounded stderr tail attached.
    """

    combined = f"{stdout}\n{stderr}"
    if _LINGERING_GATE_SIGNATURE in combined:
        return {
            "reason": "relay_proxy_lingering_required",
            "remediation": (
                "Run 'loginctl enable-linger <relay-user>' on the target host for "
                "the relay user, then retry this operation."
            ),
            "detail": _clip(combined, output_tail_bytes()),
        }
    return None


@dataclass(frozen=True)
class RelayCliReceiptField:
    """One ordered ``key=value`` framed line clio-relay printed to stdout."""

    seq: int
    key: str
    value: str
    #: Best-effort JSON decode of ``value`` when it parses as a JSON object/array
    #: (bootstrap's markers carry a JSON payload); ``None`` otherwise. ``value`` is
    #: always retained verbatim regardless -- this is a convenience projection.
    value_json: Any | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"seq": self.seq, "key": self.key, "value": self.value}
        if self.value_json is not None:
            payload["value_json"] = self.value_json
        return payload


def parse_relay_cli_stdout(text: str) -> tuple[list[RelayCliReceiptField], dict[str, Any] | None]:
    """Parse clio-relay stdout into ordered receipt fields + an optional whole document.

    ``session``/``relay-host`` commands print ONE JSON document for their whole
    output (``typer.echo(model.model_dump_json(indent=2))``) -- tried first, since a
    pretty-printed multi-line JSON body would otherwise mismatch the per-line framed
    pattern on every line. ``cluster bootstrap`` prints one ``marker=json`` framed
    line per event instead; every line matching ``KEY=VALUE`` becomes one ordered
    :class:`RelayCliReceiptField`, unknown keys passed through verbatim (no
    allowlist -- CLAUDE.md ⚑ #1: this module surfaces reality, it does not decide).
    """

    stripped = text.strip()
    if stripped:
        with suppress(json.JSONDecodeError, ValueError):
            whole = json.loads(stripped)
            if isinstance(whole, dict):
                return [], whole
    fields: list[RelayCliReceiptField] = []
    for line in text.splitlines():
        match = _FRAMED_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        value_json: Any | None = None
        candidate = value.strip()
        if candidate[:1] in "{[":
            with suppress(json.JSONDecodeError, ValueError):
                value_json = json.loads(candidate)
        fields.append(
            RelayCliReceiptField(seq=len(fields), key=key, value=value, value_json=value_json)
        )
    return fields, None


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
    state: str = STATE_RUNNING
    receipt_fields: tuple[RelayCliReceiptField, ...] = ()
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
    durable store).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RelayInstallJob] = {}

    def register(self, job: RelayInstallJob) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> RelayInstallJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def append_receipt_field(self, job_id: str, field_: RelayCliReceiptField) -> None:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            self._jobs[job_id] = replace(
                current,
                receipt_fields=(*current.receipt_fields, field_),
                updated_at=_now_iso(),
                last_output_at=_now_iso(),
            )

    def note_output(self, job_id: str, *, stderr_delta: str = "") -> None:
        """Bump the liveness clock (and optionally append to the raw stderr tail)."""

        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            now = _now_iso()
            updates: dict[str, Any] = {"updated_at": now, "last_output_at": now}
            if stderr_delta:
                combined = current.stderr_tail + stderr_delta
                updates["stderr_tail"] = _clip(combined, output_tail_bytes())
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
    argv: Sequence[str],
    executable: str,
    timeout_seconds: float,
) -> RelayInstallJob:
    """Spawn ``executable argv`` in a background thread; return the running handle now.

    The subprocess is driven to terminal on a daemon thread
    (:func:`_drive_relay_install_job`) that streams stdout/stderr line-by-line,
    folding each framed receipt line into the registry as it arrives -- the caller
    never blocks on the operation itself, only on this synchronous spawn (a Popen
    call, not the SSH dial it starts).
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
        args=(registry, job_id, proc, timeout_seconds),
        name=f"clio-relay-install-{job_id}",
        daemon=True,
    )
    thread.start()
    return job


def _drive_relay_install_job(
    registry: RelayInstallJobRegistry,
    job_id: str,
    proc: "subprocess.Popen[str]",
    timeout_seconds: float,
) -> None:
    """Background-thread driver: stream both pipes, enforce the runaway backstop.

    Two reader threads (never a single blocking ``communicate()``, which would
    withhold every line until the process exits and defeat incremental progress)
    fold stdout lines into typed receipt fields as they land and accumulate the
    bounded stderr tail. ``timeout_seconds`` is a RUNAWAY BACKSTOP, not the
    operational clock (CLAUDE.md ⚑ #6) -- it exists so a truly wedged subprocess
    cannot pin a thread and a registry slot forever, not to bound a normal SSH dial.
    """

    stdout_chunks: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            stdout_chunks.append(line)
            match = _FRAMED_LINE.match(line.rstrip("\n"))
            if match:
                key, value = match.group(1), match.group(2)
                value_json: Any | None = None
                candidate = value.strip()
                if candidate[:1] in "{[":
                    with suppress(json.JSONDecodeError, ValueError):
                        value_json = json.loads(candidate)
                current = registry.get(job_id)
                seq = len(current.receipt_fields) if current is not None else 0
                registry.append_receipt_field(
                    job_id,
                    RelayCliReceiptField(seq=seq, key=key, value=value, value_json=value_json),
                )
            else:
                registry.note_output(job_id)
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

    full_stdout = "".join(stdout_chunks)
    current = registry.get(job_id)
    stderr_tail = current.stderr_tail if current is not None else ""

    if timed_out:
        logger.warning(
            "relay install job timed out reason=relay_cli_timeout job=%s kind=%s timeout_s=%s",
            job_id,
            current.kind if current is not None else "?",
            timeout_seconds,
        )
        registry.set_terminal(
            job_id,
            state=STATE_FAILED,
            exit_code=None,
            error_reason="relay_cli_timeout",
            stdout_tail=_clip(full_stdout, output_tail_bytes()),
            stderr_tail=stderr_tail,
        )
        return

    try:
        exit_code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        exit_code = None

    _, whole_doc = parse_relay_cli_stdout(full_stdout)
    ok = exit_code == 0
    actionable = _detect_actionable_refusal(full_stdout, stderr_tail)
    registry.set_terminal(
        job_id,
        state=STATE_COMPLETED if ok else STATE_FAILED,
        exit_code=exit_code,
        error_reason=""
        if ok
        else (actionable["reason"] if actionable else "relay_cli_nonzero_exit"),
        parsed_document=whole_doc,
        actionable_refusal=actionable,
        stdout_tail=_clip(full_stdout, output_tail_bytes()),
        stderr_tail=stderr_tail,
    )


def run_bounded_relay_cli(
    argv: Sequence[str], *, kind: str, timeout_seconds: float
) -> RelayInstallJob:
    """Run one relay CLI invocation to completion within ``timeout_seconds`` (BLOCKING).

    For fast, non-SSH-dialing operations (register, doctor, installation-info,
    proxy-status) -- mirrors ``JarvisJobs._bounded``'s "wait to terminal within
    budget" shape, but purely locally: no relay task backend is involved. Callers on
    the async tool-call path MUST run this via ``asyncio.to_thread`` (it blocks the
    calling thread for up to ``timeout_seconds``); it does not touch a
    :class:`RelayInstallJobRegistry` because the result is already terminal by the
    time it returns -- there is nothing left to poll.
    """

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
            stdout_tail=_clip(stdout, output_tail_bytes()),
            stderr_tail=_clip(stderr, output_tail_bytes()),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    fields, whole_doc = parse_relay_cli_stdout(stdout)
    ok = completed.returncode == 0
    actionable = _detect_actionable_refusal(stdout, stderr)
    return RelayInstallJob(
        job_id=job_id,
        kind=kind,
        argv=tuple(argv),
        created_at=now,
        updated_at=now,
        last_output_at=now,
        state=STATE_COMPLETED if ok else STATE_FAILED,
        exit_code=completed.returncode,
        receipt_fields=tuple(fields),
        parsed_document=whole_doc,
        error_reason=""
        if ok
        else (actionable["reason"] if actionable else "relay_cli_nonzero_exit"),
        actionable_refusal=actionable,
        stdout_tail=_clip(stdout, output_tail_bytes()),
        stderr_tail=_clip(stderr, output_tail_bytes()),
    )


__all__ = [
    "RELAY_CLI_EXECUTABLE_NAME",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_NEEDS_USER_ATTENTION",
    "STATE_RUNNING",
    "TERMINAL_STATES",
    "RelayCliJobError",
    "RelayCliReceiptField",
    "RelayCliUnavailableError",
    "RelayInstallJob",
    "RelayInstallJobRegistry",
    "attention_idle_seconds",
    "bounded_timeout_seconds",
    "effective_job_state",
    "long_operation_timeout_seconds",
    "output_tail_bytes",
    "parse_relay_cli_stdout",
    "resolve_relay_cli_executable",
    "run_bounded_relay_cli",
    "start_relay_install_job",
]
