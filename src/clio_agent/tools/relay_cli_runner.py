"""Config, parsing, and typed-error surface for the DEPLOYED ``clio-relay`` CLI.

clio-relay#209 A2: expose relay cluster lifecycle operations (register, bootstrap,
status, session, proxy) as clio-agent-callable operations with typed progress. This
module resolves the ``clio-relay`` executable, resolves every install-surface config
knob (file -> env -> default), and parses its stdout into typed fields. The job
execution + registry half (spawning the subprocess, driving it to terminal on a
background thread, the in-memory job ledger) lives in the sibling owner module
``tools/relay_install_jobs.py`` -- split from this file to hold each under the
per-file size ratchet (#774); together they are the ONE execution seam these
operations use, never relay's own MCP/HTTP transport (that surface talks to a relay
DOOR that must already be reachable; this seam stands the cluster up in the first
place).

Two clio-relay stdout wire shapes exist (verified against the clio-relay source, not
guessed): ``cluster bootstrap`` prints one ``marker=json`` framed line per event
(``bootstrap_target_identity_pinned=...``, ``bootstrap_receipt_json=...``, ...);
``session``/``relay-host`` commands print ONE pretty JSON document
(``model_dump_json``). :func:`parse_relay_cli_stdout` handles both. Framing is a
DECLARED allowlist of clio-relay's own marker namespaces
(:data:`_DECLARED_MARKER_PREFIXES` -- ``bootstrap_``, ``FrpcProxy``,
``endpoint_service.``), not a generic ``KEY=VALUE`` parse: an arbitrary line that
merely LOOKS like ``key=value`` (an env echo, a ``set -x`` trace line a
misconfigured remote script relayed) must never be promoted to a
permanently-retained, model-visible "receipt field" just for matching that shape --
proven live with a planted fake token. A non-matching line still reaches the bounded
stdout tail (raw text, capped) via the job-execution module; it is only never
promoted to a structured field here. This is a structural parse of the CLI's OWN
declared output, the same class of parsing ``tools/execution.py``'s
``_structured_tool_result_error`` already does for local tool stderr -- never a
keyword guess on model prose (CLAUDE.md ⚑ #1).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from clio_agent import conf

logger = logging.getLogger(__name__)

#: The clio-relay console-script name (``[project.scripts] clio-relay = ...``).
RELAY_CLI_EXECUTABLE_NAME = "clio-relay"

STATE_RUNNING = "running"
STATE_NEEDS_USER_ATTENTION = "needs_user_attention"
STATE_COMPLETED = "completed"
#: clio-relay's own documented non-failure terminal outcome for ``session start``
#: (exit code 2: "a durable operation handle is useful for status/retry/cleanup, but
#: must never look like a successfully attached API session" -- verified against
#: clio-relay's cli_session_start.py). Reached ONLY for kind == "relay_session_start";
#: every other verb's nonzero exit stays a bare failure (M2/M3: a per-verb documented
#: exception, not a generic exit-code convention).
STATE_HANDLE_ONLY = "handle_only"
STATE_FAILED = "failed"
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_HANDLE_ONLY, STATE_FAILED})

#: Exact stderr substring clio-relay's one-pass bash scripts print when the
#: systemd user-lingering gate refuses a persistent frpc proxy install (exit 78 on
#: the remote script; clio-relay's own CLI wraps this into a bare RelayError -> its
#: own process exit 1, so the substring -- not an exit code -- is the honest signal
#: available to a subprocess caller). Verified against clio-relay's
#: frpc_proxy_scripts.py; never a keyword guess on model prose (⚑ #1) -- this is a
#: structural parse of the CLI's OWN fixed error text, the same class of stderr
#: classification tools/execution.py's _is_transient_tool_error already does.
#:
#: CROSS-REPO STRING COUPLING (M3): this ties clio-agent to clio-relay's exact
#: wording, which can drift across a clio-relay release with no compile-time
#: signal here. The honest fix is relay-side: a machine-readable marker (e.g. a
#: framed ``bootstrap_lingering_required=...`` line under the SAME declared marker
#: namespace bootstrap already uses) that this module could frame structurally
#: instead of substring-matching prose. Tracked as a relay-side follow-up, not
#: fixed in this slice.
_LINGERING_GATE_SIGNATURE = "requires systemd user lingering"

#: The SAME substring can legitimately appear in worker/bootstrap stderr for an
#: unrelated reason (a remote script sharing helper code) -- gating detection to
#: proxy_lifecycle kinds only (M3) so a bootstrap/session failure that happens to
#: mention lingering is never mis-surfaced as a proxy-specific actionable refusal.
_PROXY_LIFECYCLE_KINDS = frozenset({"relay_proxy_install", "relay_proxy_teardown"})

_FRAMED_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)=(.*)$")

#: Declared clio-relay marker-line NAMESPACES (verified against the CLI source):
#: only a line whose key starts with one of these prefixes is framed into a typed
#: receipt field (F5a). See the module docstring for the threat this closes.
_DECLARED_MARKER_PREFIXES: tuple[str, ...] = ("bootstrap_", "FrpcProxy", "endpoint_service.")

#: Hard cap on retained receipt fields per job (F3): a pathological/huge stream
#: (a real 33MB bootstrap output is not hypothetical) must not grow this list
#: without bound. Past the cap, further markers are dropped from the RETAINED list
#: (never silently -- ``RelayInstallJob.receipt_fields_truncated`` is set) while
#: they still reach the bounded raw stdout tail.
MAX_RETAINED_RECEIPT_FIELDS = 2000

#: R1 (CRITICAL, review round 2): deployment-plumbing environment names
#: REPLICATED from clio-relay's OWN declared child-process contract. Source of
#: truth: ``clio_relay.constants._BASE_CHILD_ENV_NAMES``
#: (clio-relay/src/clio_relay/constants.py) -- clio-agent cannot import
#: clio_relay (a separate top-level package), so this tuple is a deliberate,
#: drift-tested replica (see ``test_relay_cli_runner.py``'s
#: ``test_base_child_env_names_matches_clio_relay_source_of_truth``, which
#: runs unconditionally whenever ``clio_relay`` happens to be importable in
#: the dev venv, and is otherwise skipped rather than silently trusting a
#: stale copy), never an independently-invented list. The OLD 8-name
#: hand-rolled allowlist here stripped names clio-relay's own
#: ``process_environment.py::_child_env`` forwards to every subprocess IT
#: spawns (UV_CACHE_DIR/UV_TOOL_DIR resolve a uv-managed install;
#: LANG/LC_ALL/LC_CTYPE affect locale-sensitive CLI output; PATHEXT/COMSPEC/
#: WINDIR/SYSTEMDRIVE are Windows process-creation plumbing) -- gaps that
#: never showed up against a fake test CLI (which needs none of them), only
#: against the real deployed executable.
_BASE_CHILD_ENV_NAMES: tuple[str, ...] = (
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "LOGNAME",
    "NoDefaultCurrentDirectoryInExePath",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SHELL",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

#: clio-agent-SPECIFIC additions beyond relay's own declared base (F5b's
#: original rationale, kept as-is): every long operation this surface drives
#: SSH-dials, and an operator's already-loaded SSH agent key is how that dial
#: authenticates without an interactive password prompt. Not part of
#: clio-relay's own declared child-env contract (relay itself never spawns an
#: SSH-dialing child of THIS CLI invocation -- the CLI process IS the SSH
#: client), so these stay a clio-agent addition rather than folded into the
#: replicated base list above.
_ENV_ALLOWLIST_EXTRA: tuple[str, ...] = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")

#: R1: the cluster registry path is NOT part of clio-relay's per-subprocess
#: child-env contract (that list is about relay forwarding ITS OWN env to
#: children it spawns) -- it is the ``clio-relay`` CLI's own top-level input,
#: read directly off ``os.environ`` by every verb
#: (``clio_relay.cluster_config.default_registry_path`` /
#: ``CLUSTER_REGISTRY_ENV``). Unset, clio-relay falls back to
#: ``.clio-relay/clusters.json`` resolved against the SUBPROCESS's own current
#: working directory -- never clio-agent's, and never any path this surface
#: configures -- so an operator who registered a cluster against a real
#: registry file silently loses every cluster the moment this name is missing
#: from the child environment (every verb here touches the cluster registry,
#: so this is forwarded unconditionally, not gated per kind).
_CLUSTER_REGISTRY_ENV_NAME = "CLIO_RELAY_CLUSTER_REGISTRY"

#: R1: REPLICATED from clio-relay's own declared credential-shaped env names.
#: Source of truth: ``clio_relay.constants._RELAY_CREDENTIAL_ENV_NAMES``
#: (see the drift test referenced above). Never forwarded wholesale -- a
#: credential env name is only ever added to a SPECIFIC verb's allowlist in
#: :data:`_ENV_NAMES_BY_KIND` below, and only when that verb actually consumes
#: it; this tuple exists so the per-verb subsets and the drift test share one
#: declared universe instead of re-typing the names independently.
_RELAY_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "CLIO_RELAY_API_TOKEN",
    "CLIO_RELAY_FRP_TOKEN",
    "CLIO_RELAY_PROGRESS_TOKEN",
    "CLIO_RELAY_RUNTIME_METADATA_TOKEN",
    "CLIO_RELAY_STCP_SECRET",
)

#: Base OS-plumbing + cluster-registry environment forwarded to every relay
#: CLI subprocess call regardless of verb (F5b): the replicated child-env
#: contract, clio-agent's own SSH-agent addition, and the cluster registry
#: path (R1 -- unconditional, not per-kind; see its constant above). This is
#: an explicit ALLOWLIST, never the full inherited process environment --
#: proven live that the full environment leaks clio-agent's own secrets (an
#: FRP token) into the child, which neither relay's own doctrine nor this
#: surface's threat model expects.
_ENV_ALLOWLIST_BASE: tuple[str, ...] = (
    *_BASE_CHILD_ENV_NAMES,
    *_ENV_ALLOWLIST_EXTRA,
    _CLUSTER_REGISTRY_ENV_NAME,
)

#: Per-verb ADDITIONAL env names (F5b: "enumerated per tool"), forwarded on top
#: of the base allowlist. R1: session start/attach/teardown each construct
#: ``RelaySettings.from_env()`` locally (clio-relay's ``config.py``), which
#: reads ``CLIO_RELAY_API_TOKEN`` unconditionally -- ``session start``'s own
#: CLI raises a typed refusal ("CLIO_RELAY_API_TOKEN is required unless
#: --no-require-token is explicit") when it is absent and required (verified
#: against clio-relay's ``cli_session_start.py``). Only
#: ``relay-host install-proxy``/``teardown-proxy`` sets up or tears down the
#: frpc transport, so only those two kinds need the frp/stcp secret env vars
#: clio-relay's own ``FrpTransportConfig.token_env`` / ``.stcp_secret_env``
#: names (defaults ``CLIO_RELAY_FRP_TOKEN`` / ``CLIO_RELAY_STCP_SECRET`` --
#: these are PER-CLUSTER configurable in clio-relay; a caller that knows a
#: cluster's non-default names passes them via ``_subprocess_env``'s
#: ``extra_env_names``, since the cluster definition itself is not available
#: at this layer). Every other verb (register/bootstrap/status) gets the base
#: allowlist only.
_ENV_NAMES_BY_KIND: dict[str, tuple[str, ...]] = {
    "relay_session_start": ("CLIO_RELAY_API_TOKEN",),
    "relay_session_attach": ("CLIO_RELAY_API_TOKEN",),
    "relay_session_teardown": ("CLIO_RELAY_API_TOKEN",),
    "relay_proxy_install": ("CLIO_RELAY_FRP_TOKEN", "CLIO_RELAY_STCP_SECRET"),
    "relay_proxy_teardown": ("CLIO_RELAY_FRP_TOKEN", "CLIO_RELAY_STCP_SECRET"),
}


class RelayCliUnavailableError(RuntimeError):
    """The ``clio-relay`` executable could not be resolved."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "relay_cli_unavailable",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
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

    M5: an explicitly configured path is EXISTENCE-CHECKED here (typed refusal on a
    stale/typo'd config value) rather than left to surface as a raw ``OSError`` from
    whichever ``Popen``/``run`` call happens to be the first to try it.
    """

    configured = conf.resolve(
        "relay.install_surface.cli_path",
        env="CLIO_RELAY_CLI_PATH",
        default="",
        cast=conf.as_str,
    ).strip()
    if configured:
        if not Path(configured).exists():
            raise RelayCliUnavailableError(
                f"configured clio-relay executable {configured!r} does not exist",
                reason="relay_cli_configured_path_missing",
                details={"configured_path": configured},
            )
        return configured
    found = shutil.which(RELAY_CLI_EXECUTABLE_NAME)
    if not found:
        raise RelayCliUnavailableError(
            f"{RELAY_CLI_EXECUTABLE_NAME!r} was not found on PATH and "
            "relay.install_surface.cli_path is unset",
            details={"executable": RELAY_CLI_EXECUTABLE_NAME},
        )
    return found


def long_operation_timeout_seconds() -> float:
    """Runaway backstop shared by every SSH-dialing long operation this surface
    drives asynchronously (``cluster bootstrap``, ``session start``/``attach``/
    ``teardown``, ``relay-host install-proxy``/``teardown-proxy``) -- a ceiling, not
    the operational clock (CLAUDE.md ⚑ #6): a normal dial finishes in seconds to a
    few minutes, this exists only to reclaim a truly wedged subprocess."""

    return float(
        conf.resolve(
            "relay.install_surface.long_operation_timeout_seconds",
            env="CLIO_RELAY_INSTALL_LONG_OP_TIMEOUT_S",
            default=900.0,
            cast=conf.as_float,
        )
    )


def bounded_timeout_seconds() -> float:
    """Timeout for a fast, non-SSH-dialing sub-probe (register, doctor, status)."""

    return float(
        conf.resolve(
            "relay.install_surface.bounded_timeout_seconds",
            env="CLIO_RELAY_INSTALL_BOUNDED_TIMEOUT_S",
            default=60.0,
            cast=conf.as_float,
        )
    )


def attention_idle_seconds() -> float:
    """No-output duration after which a still-running job is labeled needing the
    operator's attention (an SSH/2FA prompt the CLI has no non-interactive bound
    for -- see ``docs/connection-model.md``'s "user present at bring-up" doctrine).
    This RELABELS an in-flight job's reported state; it never kills the process."""

    return float(
        conf.resolve(
            "relay.install_surface.attention_idle_seconds",
            env="CLIO_RELAY_INSTALL_ATTENTION_IDLE_S",
            default=45.0,
            cast=conf.as_float,
        )
    )


def output_tail_bytes() -> int:
    """Bound on the retained stdout/stderr tail (never an unbounded buffer)."""

    return int(
        conf.resolve(
            "relay.install_surface.output_tail_bytes",
            env="CLIO_RELAY_INSTALL_OUTPUT_TAIL_BYTES",
            default=4096,
            cast=conf.as_int,
        )
    )


def parsed_document_max_bytes() -> int:
    """Bound on the buffer used for whole-document JSON parsing on the ASYNC
    (streamed) execution path (R3, review round 2).

    Deliberately INDEPENDENT of, and much larger than, :func:`output_tail_bytes`:
    that tail is sized for a short display excerpt (default 4096 bytes), but
    ``session``/``relay-host`` commands print ONE whole JSON document as their
    entire output, and that document can legitimately exceed the display tail.
    Parsing off the already-clipped display tail silently loses
    ``parsed_document`` for any document bigger than it -- the exact bug this
    bound exists to prevent (see ``relay_install_jobs.py``'s
    ``_drive_relay_install_job``, which accumulates a SEPARATE buffer capped at
    this bound purely for document parsing). When even this larger bound is
    exceeded, parsing is skipped entirely and the job's
    ``parsed_document_truncated`` flag is set -- the loss is surfaced, never
    silent.
    """

    return int(
        conf.resolve(
            "relay.install_surface.parsed_document_max_bytes",
            env="CLIO_RELAY_INSTALL_PARSED_DOCUMENT_MAX_BYTES",
            default=262144,
            cast=conf.as_int,
        )
    )


def job_retention_max_entries() -> int:
    """Soft bound (F4): past this many tracked jobs, the oldest TERMINAL job is
    evicted first -- mirrors ``gact/runtime/retention.py``'s terminal-first policy
    shape (reimplemented locally; ``tools/`` may not import ``gact/``)."""

    return int(
        conf.resolve(
            "relay.install_surface.job_retention_max",
            env="CLIO_RELAY_INSTALL_JOB_RETENTION_MAX",
            default=200,
            cast=conf.as_int,
        )
    )


def job_retention_hard_cap() -> int:
    """Hard ceiling (F4): past this many tracked jobs even a still-RUNNING job is
    force-evicted from the registry (never killed -- only the poll handle is lost;
    see ``relay_install_jobs.py``'s module docstring's orphan-subprocess note)."""

    return int(
        conf.resolve(
            "relay.install_surface.job_retention_hard_cap",
            env="CLIO_RELAY_INSTALL_JOB_RETENTION_HARD_CAP",
            default=400,
            cast=conf.as_int,
        )
    )


def _clip(text: str, max_bytes: int) -> str:
    """Clip text to its trailing ``max_bytes`` (the tail is the actionable part)."""

    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    return raw[-max_bytes:].decode("utf-8", errors="replace")


def _is_declared_marker(key: str) -> bool:
    """True when ``key`` belongs to one of clio-relay's declared marker namespaces."""

    return key.startswith(_DECLARED_MARKER_PREFIXES)


def _detect_actionable_refusal(stdout: str, stderr: str, *, kind: str) -> dict[str, Any] | None:
    """Classify a known, structural clio-relay refusal into a typed remediation.

    Currently the one case: the systemd user-lingering gate a persistent frpc proxy
    install/teardown refuses on. Scoped to ``kind in _PROXY_LIFECYCLE_KINDS`` (M3):
    the same stderr substring can legitimately appear from a worker/bootstrap path
    for an unrelated reason, and must not be mis-surfaced as a proxy-specific
    actionable refusal there. Returns ``None`` for every other failure -- those stay
    a bare ``relay_cli_nonzero_exit`` with the bounded stderr tail attached.
    """

    if kind not in _PROXY_LIFECYCLE_KINDS:
        return None
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


#: R2 (CRITICAL, review round 2): click's own ``UsageError`` also exits 2 (a bad
#: CLI argument to ``session start``), so exit code 2 ALONE was never sufficient
#: to call the outcome a durable handle -- only a SUCCESSFULLY PARSED whole JSON
#: document (the handle-only receipt clio-relay's own ``cli_session_start.py``
#: prints on stdout before it exits 2) proves the CLI actually reached ITS OWN
#: handle-only branch rather than failing argument parsing before printing
#: anything at all. Exit 2 with no parsed document is now a bare, TYPED failure
#: -- never silently reported as a durable handle with an empty error_reason.
REASON_SESSION_START_EXIT2_UNDOCUMENTED = "relay_session_start_exit2_undocumented"


def _classify_exit_state(
    kind: str, exit_code: int | None, *, parsed_document: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Map a process exit code (+ whether a whole document parsed) to a terminal state.

    M2: ``clio-relay session start`` exits 2 for a genuinely non-failure outcome --
    a durable handle whose API session is not yet usable/attached (verified against
    clio-relay's ``cli_session_start.py``: "a durable operation handle is useful
    for status/retry/cleanup, but must never look like a successfully attached API
    session"). This is a dedicated, per-verb documented wire meaning, not a generic
    exit-code convention -- ONLY ``kind == "relay_session_start"`` gets it, and
    (R2) only when ``parsed_document`` -- the caller's own
    :func:`parse_relay_cli_stdout` result over the UNCLIPPED stdout -- is not
    ``None``.

    Returns ``(state, reason_override)``. ``reason_override`` is ``""`` in every
    case except the R2 exit-2-without-a-parsed-document one, where it names the
    specific typed reason; a caller building ``error_reason`` for a FAILED state
    should prefer a non-empty ``reason_override`` over its own generic
    ``relay_cli_nonzero_exit`` fallback.
    """

    if exit_code == 0:
        return STATE_COMPLETED, ""
    if kind == "relay_session_start" and exit_code == 2:
        if parsed_document is not None:
            return STATE_HANDLE_ONLY, ""
        return STATE_FAILED, REASON_SESSION_START_EXIT2_UNDOCUMENTED
    return STATE_FAILED, ""


def _subprocess_env(kind: str, *, extra_env_names: Sequence[str] = ()) -> dict[str, str]:
    """Build the explicit allowlisted environment for one relay CLI subprocess (F5b).

    R1: iterates the ALLOWED NAME list and looks each value up via
    ``name in os.environ`` / ``os.environ[name]`` -- mirroring clio-relay's own
    ``process_environment.py::_child_env`` shape -- rather than filtering
    ``os.environ.items()`` by literal key membership. On Windows, ``os.environ``
    normalizes ``in`` / ``__getitem__`` lookups case-insensitively, but the RAW
    casing ``.items()`` yields depends on how the parent process's own
    environment block happened to be populated (observed live on this box:
    ``"SYSTEMROOT"``, not ``"SystemRoot"``) -- so the old items()-filtering shape
    could silently drop a correctly-named allowlist entry whenever the calling
    shell's casing convention did not match this module's literal spelling. This
    shape is immune to that: membership and lookup both go through Windows'
    own case-insensitive ``os.environ``, exactly like clio-relay's own code does.

    ``extra_env_names`` (R1's per-cluster-configurable-name bullet) lets a caller
    that KNOWS the target cluster's non-default frp/stcp secret env NAMES
    (``FrpTransportConfig.token_env`` / ``.stcp_secret_env`` are per-cluster
    configurable in clio-relay) forward those specific names for just this one
    call -- the cluster definition itself is not available at this layer.
    """

    allowed = {
        *_ENV_ALLOWLIST_BASE,
        *_ENV_NAMES_BY_KIND.get(kind, ()),
        *extra_env_names,
    }
    return {name: os.environ[name] for name in allowed if name in os.environ}


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


def _parse_marker_line(line: str) -> RelayCliReceiptField | None:
    """Parse ONE stdout line into a receipt field iff it is a declared marker."""

    match = _FRAMED_LINE.match(line)
    if not match:
        return None
    key, value = match.group(1), match.group(2)
    if not _is_declared_marker(key):
        return None
    value_json: Any | None = None
    candidate = value.strip()
    if candidate[:1] in "{[":
        with suppress(json.JSONDecodeError, ValueError):
            value_json = json.loads(candidate)
    return RelayCliReceiptField(
        seq=0, key=key, value=value, value_json=value_json
    )  # seq assigned by caller


def _is_unrecognized_framed_line(line: str) -> bool:
    """True when ``line`` matches the ``key=value`` framed SHAPE but its key is
    not under any declared clio-relay marker namespace (R4/small fix from the
    review's R4 note): distinguishes a genuinely unrecognized marker -- likely
    schema drift against a newer clio-relay release that added a namespace this
    build does not yet know -- from ordinary prose that never matched the framed
    shape at all. Never used to promote or retain the line's CONTENT (F5a stays
    intact); only to COUNT how many were seen (see
    :data:`RelayInstallJob.unrecognized_marker_count`
    <clio_agent.tools.relay_install_jobs.RelayInstallJob>`), so the drop is
    typed/queryable instead of silent, without expanding the declared namespace
    list.
    """

    match = _FRAMED_LINE.match(line)
    if not match:
        return False
    return not _is_declared_marker(match.group(1))


def parse_relay_cli_stdout(
    text: str,
) -> tuple[list[RelayCliReceiptField], dict[str, Any] | None, int]:
    """Parse clio-relay stdout into ordered receipt fields + an optional whole document.

    ``session``/``relay-host`` commands print ONE JSON document for their whole
    output (``typer.echo(model.model_dump_json(indent=2))``) -- tried first, since a
    pretty-printed multi-line JSON body would otherwise mismatch the per-line framed
    pattern on every line. ``cluster bootstrap`` prints one ``marker=json`` framed
    line per event instead; every line whose key matches a DECLARED marker namespace
    (:data:`_DECLARED_MARKER_PREFIXES`) becomes one ordered
    :class:`RelayCliReceiptField`; a line that merely looks like ``key=value`` but
    is not a declared marker is not (F5a). Capped at
    :data:`MAX_RETAINED_RECEIPT_FIELDS`; the caller learns of truncation via the
    returned field count.

    Returns ``(fields, whole_document, unrecognized_marker_count)``. The third
    element is R4's typed telemetry: a COUNT of lines that matched the framed
    ``key=value`` shape but were not under a declared marker namespace -- never
    their content (F5a still never promotes that), just a count so a caller can
    surface it (mirroring how :data:`RelayInstallJob.receipt_fields_truncated`
    already surfaces the count-cap drop) instead of the drop being invisible.
    """

    stripped = text.strip()
    if stripped:
        with suppress(json.JSONDecodeError, ValueError):
            whole = json.loads(stripped)
            if isinstance(whole, dict):
                return [], whole, 0
    fields: list[RelayCliReceiptField] = []
    unrecognized_marker_count = 0
    for line in text.splitlines():
        parsed = _parse_marker_line(line)
        if parsed is None:
            if _is_unrecognized_framed_line(line):
                unrecognized_marker_count += 1
            continue
        if len(fields) >= MAX_RETAINED_RECEIPT_FIELDS:
            break
        fields.append(replace(parsed, seq=len(fields)))
    return fields, None, unrecognized_marker_count


__all__ = [
    "MAX_RETAINED_RECEIPT_FIELDS",
    "RELAY_CLI_EXECUTABLE_NAME",
    "REASON_SESSION_START_EXIT2_UNDOCUMENTED",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_HANDLE_ONLY",
    "STATE_NEEDS_USER_ATTENTION",
    "STATE_RUNNING",
    "TERMINAL_STATES",
    "RelayCliJobError",
    "RelayCliReceiptField",
    "RelayCliUnavailableError",
    "attention_idle_seconds",
    "bounded_timeout_seconds",
    "job_retention_hard_cap",
    "job_retention_max_entries",
    "long_operation_timeout_seconds",
    "output_tail_bytes",
    "parse_relay_cli_stdout",
    "parsed_document_max_bytes",
    "resolve_relay_cli_executable",
]
