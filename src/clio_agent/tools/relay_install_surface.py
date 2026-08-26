"""Curated clio-relay cluster-lifecycle tools (clio-relay#209 A2).

Five tools over the DEPLOYED ``clio-relay`` CLI, run as a local subprocess via
``tools/relay_cli_runner.py`` (config/parsing) and ``tools/relay_install_jobs.py``
(job execution + registry): register a cluster, bootstrap it, compose its status,
drive a session's lifecycle, and drive its frpc proxy's lifecycle. This is the
backend the future infrastructure-management UI (codex's campaign) drives -- these
tools never touch relay's own MCP/HTTP transport (``relay_transport.py``,
``jarvis_jobs.py``, ``remote_mcp.py``): that surface talks to a relay DOOR that must
already be reachable; this surface stands the cluster up in the first place.

Long, SSH-dialing operations (bootstrap; session start/attach/teardown; proxy
install/teardown) are handle-first: the tool call returns a job handle immediately
and the caller polls the SAME tool with ``action="status"``. Fast, non-dialing
operations (register; status composition) run to completion within a bounded
timeout, mirroring ``JarvisJobs._bounded``'s shape.

M4: every anticipated refusal (bad arguments, an unknown job id, the CLI itself
unavailable, a duplicate in-flight run, an unconfirmed cluster overwrite) is
returned as a normal terminal tool RESULT, never raised as a Python exception past
``invoke()`` -- an exception raised out of a FastMCP ``Tool.run()`` is flattened to
prose by the calling framework and never reaches the trace/API as structured, typed
content. ``cluster_register``/``cluster_bootstrap``/``session_lifecycle``/
``proxy_lifecycle`` raise :class:`~clio_agent.tools.relay_cli_runner.RelayCliJobError`
/ ``RelayCliUnavailableError`` internally for their OWN validation; :meth:`invoke`
is the ONE place that catches and renders them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations

from clio_agent.tools.relay_cli_runner import (
    STATE_COMPLETED,
    STATE_FAILED,
    RelayCliJobError,
    RelayCliUnavailableError,
    attention_idle_seconds,
    bounded_timeout_seconds,
    long_operation_timeout_seconds,
    resolve_relay_cli_executable,
)
from clio_agent.tools.relay_install_jobs import (
    RelayInstallJob,
    RelayInstallJobRegistry,
    effective_job_state,
    run_bounded_relay_cli,
    start_relay_install_job,
)

RELAY_INSTALL_NAMESPACE = "relay_ops"
RELAY_INSTALL_TOOL_NAMES = (
    "relay_cluster_register",
    "relay_cluster_bootstrap",
    "relay_cluster_status",
    "relay_session_lifecycle",
    "relay_proxy_lifecycle",
)

#: What a confirmed ``replace=true`` re-registration resets to CLI defaults (F2):
#: ``cluster add`` is clio-relay's own FULL REPLACE of the stored
#: ``ClusterDefinition`` (``registry.clusters[name] = definition``, never a merge --
#: verified against clio-relay's ``cli_cluster.py``), and this curated tool does not
#: expose target_identity/frp_transport/jarvis-spack-agent-bin fields at all, so ANY
#: re-registration through it resets them to the CLI's own defaults, never preserves
#: whatever a prior ``pin-target``/``pin-runtime``/hand-authored ``cluster add`` set.
_REPLACE_RESET_WARNING = (
    "target_identity (the SSH host-key trust pin), frp_transport, jarvis/spack/agent "
    "executable paths, and worker capacity"
)

_JOB_HANDLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "kind": {"type": "string"},
        "state": {
            "type": "string",
            "enum": ["running", "needs_user_attention", "handle_only", "completed", "failed"],
        },
        "terminal": {"type": "boolean"},
        "exit_code": {"type": ["integer", "null"]},
        "receipt_fields": {"type": "array"},
        "receipt_fields_truncated": {"type": "boolean"},
        "unrecognized_marker_count": {"type": "integer"},
        "parsed_document": {"type": ["object", "null"]},
        "parsed_document_truncated": {"type": "boolean"},
        "stdout_tail": {"type": "string"},
        "stderr_tail": {"type": "string"},
        "error_reason": {"type": "string"},
        "actionable_refusal": {"type": ["object", "null"]},
    },
    "required": ["job_id", "kind", "state", "terminal"],
    "additionalProperties": False,
}

_STATUS_DOC_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cluster": {"type": "string"},
        "doctor": _JOB_HANDLE_OUTPUT_SCHEMA,
        "installation_info": _JOB_HANDLE_OUTPUT_SCHEMA,
        "proxy_status": _JOB_HANDLE_OUTPUT_SCHEMA,
    },
    "required": ["cluster", "doctor", "installation_info", "proxy_status"],
    "additionalProperties": False,
}

_IDENTITY = {"type": "string", "minLength": 1, "maxLength": 256}
_OPTIONAL_IDENTITY = {"anyOf": [_IDENTITY, {"type": "null"}], "default": None}
_JOB_ID = {"type": "string", "minLength": 1, "maxLength": 64}


def _optional_env_name(description: str) -> dict[str, Any]:
    """R1: an optional env var NAME override field (never a secret value)."""

    return {"anyOf": [_IDENTITY, {"type": "null"}], "default": None, "description": description}


_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "relay_cluster_register": {
        "type": "object",
        "properties": {
            "cluster": _IDENTITY,
            "ssh_host": _IDENTITY,
            "bootstrap_profile": {"type": "string", "default": "linux-user"},
            "core_dir": _OPTIONAL_IDENTITY,
            "spool_dir": _OPTIONAL_IDENTITY,
            "replace": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Required to overwrite an ALREADY-registered cluster. clio-relay's "
                    "own 'cluster add' is a full replace, never a merge -- confirming "
                    f"this resets {_REPLACE_RESET_WARNING} to their defaults."
                ),
            },
        },
        "required": ["cluster", "ssh_host"],
        "additionalProperties": False,
    },
    "relay_cluster_bootstrap": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "status"], "default": "start"},
            "cluster": _OPTIONAL_IDENTITY,
            "ssh_host": _OPTIONAL_IDENTITY,
            "relay_wheel": _OPTIONAL_IDENTITY,
            "relay_artifact_sha256": _OPTIONAL_IDENTITY,
            "report_path": _OPTIONAL_IDENTITY,
            "validation_launcher": _OPTIONAL_IDENTITY,
            "validation_install_source": _OPTIONAL_IDENTITY,
            "job_id": {"anyOf": [_JOB_ID, {"type": "null"}], "default": None},
        },
        "required": [],
        "additionalProperties": False,
    },
    "relay_cluster_status": {
        "type": "object",
        "properties": {
            "cluster": _IDENTITY,
            "ssh_host": _OPTIONAL_IDENTITY,
        },
        "required": ["cluster"],
        "additionalProperties": False,
    },
    "relay_session_lifecycle": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "attach", "teardown", "status"],
            },
            "cluster": _OPTIONAL_IDENTITY,
            "session_id": _OPTIONAL_IDENTITY,
            "remote_api_port": {"type": "integer", "default": 8765},
            "replace": {"type": "boolean", "default": False},
            "require_token": {"type": "boolean", "default": True},
            "start_operation_id": _OPTIONAL_IDENTITY,
            "expected_cluster_route_revision": _OPTIONAL_IDENTITY,
            "expected_api_release_identity_sha256": _OPTIONAL_IDENTITY,
            "stop_worker": {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None},
            "cancel_jobs": {"type": "boolean", "default": False},
            "cancel_scheduler_jobs": {"type": "boolean", "default": False},
            "preserve_scheduler_job_id": {
                "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
                "default": None,
            },
            "relay_cancel_timeout_seconds": {"type": "number", "default": 30.0},
            "relay_cancel_poll_seconds": {"type": "number", "default": 0.25},
            "job_id": {"anyOf": [_JOB_ID, {"type": "null"}], "default": None},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "relay_proxy_lifecycle": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["install_proxy", "teardown_proxy", "status"]},
            "cluster": _OPTIONAL_IDENTITY,
            "ssh_host": _OPTIONAL_IDENTITY,
            "remote_port": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            "require_persistent": {"type": "boolean", "default": True},
            "frp_token_env": _optional_env_name(
                "Override the frp token env var NAME (non-default "
                "frp_transport.token_env) -- names the var holding the secret, "
                "never the secret value itself."
            ),
            "stcp_secret_env": _optional_env_name(
                "Override the stcp secret env var NAME (non-default "
                "frp_transport.stcp_secret_env) -- names the var, never the "
                "secret value itself."
            ),
            "job_id": {"anyOf": [_JOB_ID, {"type": "null"}], "default": None},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

_TITLES = {
    "relay_cluster_register": "Register Cluster",
    "relay_cluster_bootstrap": "Bootstrap Cluster",
    "relay_cluster_status": "Cluster Status",
    "relay_session_lifecycle": "Session Lifecycle",
    "relay_proxy_lifecycle": "Proxy Lifecycle",
}

_DESCRIPTIONS = {
    "relay_cluster_register": (
        "Use this before a cluster can be bootstrapped or targeted by any other "
        "relay operation -- registers a NEW cluster's SSH connection and deployment "
        "profile in clio-relay's local registry. Fast local write; no SSH connection "
        "is made. REFUSES if the cluster is already registered unless replace=true is "
        f"passed -- confirming a replace resets {_REPLACE_RESET_WARNING} to defaults, "
        "since clio-relay's own registration verb is a full replace, not a merge."
    ),
    "relay_cluster_bootstrap": (
        "Use this to bring up (or repair) a registered cluster's relay runtime "
        "over SSH -- a multi-minute, two-dial operation that may pause for the "
        "operator's own SSH/2FA prompt (clio-relay has no non-interactive bound "
        "for this dial). Returns a job handle immediately (action='start'); poll "
        "it back with action='status' and the same job_id to see its framed "
        "receipt lines (identity pin trust=first_use, preflight, install receipt) "
        "as they land. A 'needs_user_attention' state means the operator likely "
        "needs to answer a prompt out of band -- the job is never killed for it."
    ),
    "relay_cluster_status": (
        "Use this to get one composed, typed picture of a registered cluster's "
        "current state: doctor diagnostics, this deployment's installation-info, "
        "and the cluster's frpc proxy status. Each sub-probe is bounded and "
        "reported independently -- one failing does not fail the others."
    ),
    "relay_session_lifecycle": (
        "Use this to start, attach to, or tear down an owned relay session on a "
        "registered cluster -- action selects the verb. start/attach/teardown may "
        "SSH-dial and return a job handle immediately; poll with action='status' "
        "and the same job_id. A 'handle_only' terminal state from start means a "
        "durable handle exists but the API session is not yet usable/attached -- "
        "this is clio-relay's own documented non-failure outcome, not an error."
    ),
    "relay_proxy_lifecycle": (
        "Use this to install or tear down a cluster's frpc proxy -- action "
        "selects the verb. Both may SSH-dial and return a job handle immediately; "
        "poll with action='status' and the same job_id. A persistent install "
        "refused by the systemd user-lingering gate surfaces as a typed "
        "actionable_refusal naming the 'loginctl enable-linger' remediation, "
        "never a bare failure. Non-default frp_transport token/secret env var "
        "NAMES: pass frp_token_env/stcp_secret_env (default CLIO_RELAY_FRP_TOKEN/"
        "CLIO_RELAY_STCP_SECRET)."
    ),
}


class _ProjectedRelayInstallTool(Tool):
    """One curated relay-install operation exposed below the gateway's mount."""

    def __init__(self, name: str, owner: "RelayInstallSurface") -> None:
        super().__init__(
            name=name.removeprefix("relay_"),
            title=_TITLES[name],
            description=_DESCRIPTIONS[name],
            parameters=deepcopy(_INPUT_SCHEMAS[name]),
            output_schema=deepcopy(
                _STATUS_DOC_OUTPUT_SCHEMA
                if name == "relay_cluster_status"
                else _JOB_HANDLE_OUTPUT_SCHEMA
            ),
            annotations=ToolAnnotations(
                read_only_hint=name in {"relay_cluster_status"},
                # F2: relay_cluster_register can now perform a full destructive
                # replace (replace=true) just like the session/proxy lifecycle
                # tools already could -- every mutating tool here declares it.
                destructive_hint=name != "relay_cluster_status",
                # None of the five are safely idempotent: register REFUSES a
                # second call against an existing cluster (bad F2) rather than
                # no-op'ing, and every other verb can transition live remote
                # state differently depending on what is already running.
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        self._relay_name = name
        self._owner = owner

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Invoke the owner while preserving typed errors and structured output."""

        result = await self._owner.invoke(self._relay_name, arguments)
        return ToolResult(structured_content=result)


def _flag_pair(value: bool | None, *, true_flag: str, false_flag: str) -> list[str]:
    if value is None:
        return []
    return [true_flag] if value else [false_flag]


def _refusal_job_dict(exc: Exception, *, kind: str) -> dict[str, Any]:
    """Render a rejected operation as a terminal job-handle dict (M4).

    ``details["job_id"]`` (set by the M8 duplicate-run guard) is threaded through
    so the caller can immediately poll the ALREADY-live job instead of re-deriving
    it from prose.
    """

    reason = getattr(exc, "reason", "relay_install_error")
    details = getattr(exc, "details", None) or {}
    job_id = str(details.get("job_id") or "")
    return {
        "job_id": job_id,
        "kind": kind,
        "state": STATE_FAILED,
        "terminal": True,
        "exit_code": None,
        "receipt_fields": [],
        "receipt_fields_truncated": False,
        "unrecognized_marker_count": 0,
        "parsed_document": None,
        "parsed_document_truncated": False,
        "stdout_tail": "",
        "stderr_tail": "",
        "error_reason": reason,
        "actionable_refusal": None,
    }


def _cluster_is_registered(cluster_list_stdout: str, cluster: str) -> bool:
    """True when ``cluster`` appears as a registered cluster in ``cluster list`` output.

    clio-relay's ``cluster list`` prints one plain-text line per cluster,
    ``"{name} ssh=... profile=... worker_concurrency=... control_query_concurrency=..."``
    (verified against clio-relay's ``cli_cluster.py``) -- matched by an EXACT name
    prefix (``"{cluster} ssh="``) so a name that is merely a substring of another
    registered cluster's name never false-positives.
    """

    prefix = f"{cluster} ssh="
    return any(line.strip().startswith(prefix) for line in cluster_list_stdout.splitlines())


def _custom_credential_env_names(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """R1: forward a cluster's NON-DEFAULT frp/stcp secret env var NAMES --
    ``FrpTransportConfig.token_env``/``.stcp_secret_env``, not resolvable at
    this layer, so a caller that knows the NAME passes it explicitly (never
    the secret value, which must already be set under that name)."""

    values = (arguments.get("frp_token_env"), arguments.get("stcp_secret_env"))
    return tuple(v.strip() for v in values if isinstance(v, str) and v.strip())


class RelayInstallSurface:
    """Five curated clio-relay cluster-lifecycle tools over the local CLI subprocess."""

    def __init__(
        self,
        *,
        job_registry: RelayInstallJobRegistry | None = None,
        cli_status: Mapping[str, Any] | None = None,
    ) -> None:
        """Construct the five curated tools.

        Args:
            job_registry: Injected job ledger (tests supply a fresh one per
                case, keeping isolation); defaults to a FRESH per-instance
                registry when omitted. M7: production construction
                (``relay_factory.py::_build_relay_install_surface``) instead
                passes the process-wide ``default_relay_install_job_registry()``
                singleton explicitly, so a job survives a TTL catalog refresh.
            cli_status: Typed boot-time resolution status for the ``clio-relay``
                executable, retained for diagnostics only -- every tool call
                re-resolves the executable itself rather than trusting it.
        """

        self._jobs = job_registry if job_registry is not None else RelayInstallJobRegistry()
        self._cli_status = dict(cli_status or {})
        server = FastMCP("clio-relay-install")
        for name in RELAY_INSTALL_TOOL_NAMES:
            server.add_tool(_ProjectedRelayInstallTool(name, self))
        self._server = server

    @property
    def server(self) -> FastMCP:
        """Return the bare-name server mounted under the relay_ops namespace."""
        return self._server

    @property
    def status(self) -> dict[str, Any]:
        """Return the retained boot-time CLI-resolution status (diagnostics only)."""
        return dict(self._cli_status)

    async def invoke(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one of the five curated tool names.

        M4: every :class:`RelayCliJobError` / :class:`RelayCliUnavailableError`
        raised by a dispatch method is caught HERE and rendered as a terminal tool
        result (never left to propagate and be flattened to prose by the calling
        framework). ``relay_cluster_status`` declares a DIFFERENT output shape (a
        composed document, not a bare job handle), so its refusal is rendered into
        that same three-way-composed shape instead.
        """

        try:
            if tool_name == "relay_cluster_register":
                return await self.cluster_register(arguments)
            if tool_name == "relay_cluster_bootstrap":
                return await self.cluster_bootstrap(arguments)
            if tool_name == "relay_cluster_status":
                return await self.cluster_status(arguments)
            if tool_name == "relay_session_lifecycle":
                return await self.session_lifecycle(arguments)
            if tool_name == "relay_proxy_lifecycle":
                return await self.proxy_lifecycle(arguments)
        except (RelayCliJobError, RelayCliUnavailableError) as exc:
            if tool_name == "relay_cluster_status":
                refusal = _refusal_job_dict(exc, kind="relay_cluster_status")
                return {
                    "cluster": str(arguments.get("cluster") or ""),
                    "doctor": refusal,
                    "installation_info": refusal,
                    "proxy_status": refusal,
                }
            return _refusal_job_dict(exc, kind=tool_name)
        raise ValueError(f"unsupported curated relay install tool: {tool_name!r}")

    # -- register -------------------------------------------------------- #

    async def cluster_register(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Register a NEW cluster definition, or replace an existing one (F2).

        Bounded, no SSH dial. REFUSES an already-registered cluster unless
        ``replace=true`` -- clio-relay's own ``cluster add`` is a full replace of
        the stored definition (never a merge), and this curated tool does not
        expose target_identity/frp_transport/jarvis-spack-agent-bin fields at all,
        so a silent re-registration would reset every one of them to defaults with
        no warning (the #1244-class bug this ruling fixes).
        """

        cluster = _require_str(arguments, "cluster")
        ssh_host = _require_str(arguments, "ssh_host")
        if not bool(arguments.get("replace", False)):
            existing = await asyncio.to_thread(
                run_bounded_relay_cli,
                ["cluster", "list"],
                kind="relay_cluster_list",
                timeout_seconds=bounded_timeout_seconds(),
                # F2 correctness: the default output tail is sized for a receipt
                # excerpt, not a full cluster-registry enumeration -- a truncated
                # match here would let the exact overwrite this check exists to
                # prevent slip through silently for a large registry.
                tail_bytes=1 << 16,
            )
            if existing.state == STATE_COMPLETED and _cluster_is_registered(
                existing.stdout_tail, cluster
            ):
                raise RelayCliJobError(
                    f"cluster {cluster!r} is already registered; pass replace=true to "
                    f"overwrite it (this RESETS {_REPLACE_RESET_WARNING} to their "
                    "defaults -- clio-relay's 'cluster add' is a full replace, not a "
                    "merge)",
                    reason="relay_cluster_already_registered",
                    details={"cluster": cluster},
                )
        argv = [
            "cluster",
            "add",
            "--name",
            cluster,
            "--ssh-host",
            ssh_host,
            "--bootstrap-profile",
            str(arguments.get("bootstrap_profile") or "linux-user"),
        ]
        if arguments.get("core_dir"):
            argv += ["--core-dir", str(arguments["core_dir"])]
        if arguments.get("spool_dir"):
            argv += ["--spool-dir", str(arguments["spool_dir"])]
        job = await asyncio.to_thread(
            run_bounded_relay_cli,
            argv,
            kind="relay_cluster_register",
            timeout_seconds=bounded_timeout_seconds(),
        )
        return job.to_wire()

    # -- bootstrap --------------------------------------------------------- #

    async def cluster_bootstrap(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Start ('start') or poll ('status') a cluster bootstrap job."""

        action = str(arguments.get("action") or "start")
        if action == "status":
            return self._poll(_require_str(arguments, "job_id"))
        if action != "start":
            raise RelayCliJobError(
                f"unsupported relay_cluster_bootstrap action {action!r}",
                reason="relay_install_action_invalid",
                details={"action": action},
            )
        cluster = _require_str(arguments, "cluster")
        argv = ["cluster", "bootstrap", "--cluster", cluster]
        if arguments.get("ssh_host"):
            argv += ["--ssh-host", str(arguments["ssh_host"])]
        wheel = arguments.get("relay_wheel")
        sha = arguments.get("relay_artifact_sha256")
        # M1: clio-relay's real rule is asymmetric -- a wheel REQUIRES its sha (an
        # unverified custom wheel is never accepted), but a sha ALONE is a valid,
        # documented release-pinning path (verify a resolved wheel against a known
        # sha without also overriding its source). The prior symmetric XOR check
        # blocked that legitimate sha-only path.
        if wheel and not sha:
            raise RelayCliJobError(
                "relay_artifact_sha256 is required when relay_wheel is supplied",
                reason="relay_install_arguments_invalid",
                details={"relay_wheel": True, "relay_artifact_sha256": False},
            )
        if wheel:
            argv += ["--relay-wheel", str(wheel), "--relay-artifact-sha256", str(sha)]
        elif sha:
            argv += ["--relay-artifact-sha256", str(sha)]
        if arguments.get("report_path"):
            argv += ["--report", str(arguments["report_path"])]
        if arguments.get("validation_launcher"):
            argv += ["--validation-launcher", str(arguments["validation_launcher"])]
        if arguments.get("validation_install_source"):
            argv += ["--validation-install-source", str(arguments["validation_install_source"])]
        return self._start(kind="relay_cluster_bootstrap", cluster=cluster, argv=argv)

    # -- status ------------------------------------------------------------ #

    async def cluster_status(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Compose doctor + installation-info + proxy-status for one cluster."""

        cluster = _require_str(arguments, "cluster")
        ssh_host = arguments.get("ssh_host")
        doctor_argv = ["doctor", "--cluster", cluster]
        info_argv = ["installation-info"]
        proxy_argv = ["relay-host", "proxy-status", "--cluster", cluster]
        if ssh_host:
            proxy_argv += ["--ssh-host", str(ssh_host)]
        timeout = bounded_timeout_seconds()
        # M5b: return_exceptions=True -- one sub-probe's spawn/resolve exception
        # (e.g. the CLI vanishing between calls) must not discard the OTHER TWO
        # in-flight probes' results; each is reported independently regardless.
        results = await asyncio.gather(
            asyncio.to_thread(
                run_bounded_relay_cli, doctor_argv, kind="relay_doctor", timeout_seconds=timeout
            ),
            asyncio.to_thread(
                run_bounded_relay_cli,
                info_argv,
                kind="relay_installation_info",
                timeout_seconds=timeout,
            ),
            asyncio.to_thread(
                run_bounded_relay_cli,
                proxy_argv,
                kind="relay_proxy_status",
                timeout_seconds=timeout,
            ),
            return_exceptions=True,
        )
        kinds = ("relay_doctor", "relay_installation_info", "relay_proxy_status")
        rendered = [
            _refusal_job_dict(result, kind=kind)
            if isinstance(result, Exception)
            else result.to_wire()
            for result, kind in zip(results, kinds, strict=True)
        ]
        doctor, info, proxy = rendered
        return {
            "cluster": cluster,
            "doctor": doctor,
            "installation_info": info,
            "proxy_status": proxy,
        }

    # -- session ------------------------------------------------------------ #

    async def session_lifecycle(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one session verb (start/attach/teardown) or poll a prior job."""

        action = _require_str(arguments, "action")
        if action == "status":
            return self._poll(_require_str(arguments, "job_id"))
        cluster = _require_str(arguments, "cluster")
        if action == "start":
            session_id = _require_str(arguments, "session_id")
            argv = [
                "session",
                "start",
                "--cluster",
                cluster,
                "--session-id",
                session_id,
                "--remote-api-port",
                str(int(arguments.get("remote_api_port", 8765))),
            ]
            argv += _flag_pair(
                bool(arguments.get("replace", False)),
                true_flag="--replace",
                false_flag="--no-replace",
            )
            argv += _flag_pair(
                bool(arguments.get("require_token", True)),
                true_flag="--require-token",
                false_flag="--no-require-token",
            )
            for flag, key in (
                ("--start-operation-id", "start_operation_id"),
                ("--expected-cluster-route-revision", "expected_cluster_route_revision"),
                ("--expected-api-release-identity-sha256", "expected_api_release_identity_sha256"),
            ):
                if arguments.get(key):
                    argv += [flag, str(arguments[key])]
            return self._start(kind="relay_session_start", cluster=cluster, argv=argv)
        if action == "attach":
            return self._start(
                kind="relay_session_attach",
                cluster=cluster,
                argv=["session", "attach", "--cluster", cluster],
            )
        if action == "teardown":
            session_id = _require_str(arguments, "session_id")
            argv = ["session", "teardown", "--cluster", cluster, "--session-id", session_id]
            # Confirmed clio-relay flag pair (both spellings exist): forward the
            # tri-state as-is -- True/False/unset(None) each has a real meaning.
            argv += _flag_pair(
                arguments.get("stop_worker"),
                true_flag="--stop-worker",
                false_flag="--no-stop-worker",
            )
            cancel_jobs = bool(arguments.get("cancel_jobs", False))
            cancel_scheduler = bool(arguments.get("cancel_scheduler_jobs", False))
            if cancel_scheduler and not cancel_jobs:
                raise RelayCliJobError(
                    "cancel_scheduler_jobs requires cancel_jobs",
                    reason="relay_install_arguments_invalid",
                    details={"cancel_jobs": cancel_jobs, "cancel_scheduler_jobs": cancel_scheduler},
                )
            argv += ["--cancel-jobs"] if cancel_jobs else ["--keep-jobs"]
            if cancel_scheduler:
                argv += ["--cancel-scheduler-jobs"]
                for job_id in arguments.get("preserve_scheduler_job_id") or []:
                    argv += ["--preserve-scheduler-job-id", str(job_id)]
            argv += [
                "--relay-cancel-timeout-seconds",
                str(float(arguments.get("relay_cancel_timeout_seconds", 30.0))),
                "--relay-cancel-poll-seconds",
                str(float(arguments.get("relay_cancel_poll_seconds", 0.25))),
            ]
            return self._start(kind="relay_session_teardown", cluster=cluster, argv=argv)
        raise RelayCliJobError(
            f"unsupported relay_session_lifecycle action {action!r}",
            reason="relay_install_action_invalid",
            details={"action": action},
        )

    # -- proxy ------------------------------------------------------------ #

    async def proxy_lifecycle(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one proxy verb (install/teardown) or poll a prior job."""

        action = _require_str(arguments, "action")
        if action == "status":
            return self._poll(_require_str(arguments, "job_id"))
        cluster = _require_str(arguments, "cluster")
        extra_env_names = _custom_credential_env_names(arguments)

        def _spawn(kind: str, argv: list[str]) -> dict[str, Any]:
            return self._start(
                kind=kind, cluster=cluster, argv=argv, extra_env_names=extra_env_names
            )

        if action == "install_proxy":
            argv = ["relay-host", "install-proxy", "--cluster", cluster]
            if arguments.get("ssh_host"):
                argv += ["--ssh-host", str(arguments["ssh_host"])]
            if arguments.get("remote_port") is not None:
                argv += ["--remote-port", str(int(arguments["remote_port"]))]
            argv += _flag_pair(
                bool(arguments.get("require_persistent", True)),
                true_flag="--require-persistent",
                false_flag="--allow-login-scoped",
            )
            return _spawn("relay_proxy_install", argv)
        if action == "teardown_proxy":
            argv = ["relay-host", "teardown-proxy", "--cluster", cluster]
            if arguments.get("ssh_host"):
                argv += ["--ssh-host", str(arguments["ssh_host"])]
            return _spawn("relay_proxy_teardown", argv)
        raise RelayCliJobError(
            f"unsupported relay_proxy_lifecycle action {action!r}",
            reason="relay_install_action_invalid",
            details={"action": action},
        )

    # -- shared job plumbing ------------------------------------------------ #

    def _start(
        self,
        *,
        kind: str,
        cluster: str,
        argv: list[str],
        extra_env_names: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Spawn one long, SSH-dialing operation and return its handle immediately.

        M8: refuses a SECOND concurrent run for the same ``(cluster, kind)`` --
        the typed refusal names the ALREADY-live ``job_id`` so the caller polls it
        instead of guessing whether the first call is still in flight.
        ``extra_env_names`` (R1) threads through caller-known credential env
        NAMES this surface cannot resolve on its own (the cluster definition is
        not available at this layer -- see :func:`_custom_credential_env_names`).
        """

        existing = self._jobs.find_running(cluster=cluster, kind=kind)
        if existing is not None:
            raise RelayCliJobError(
                f"a {kind} job is already running for cluster {cluster!r}",
                reason="relay_install_job_already_running",
                details={"cluster": cluster, "kind": kind, "job_id": existing.job_id},
            )
        executable = resolve_relay_cli_executable()
        job = start_relay_install_job(
            self._jobs,
            kind=kind,
            cluster=cluster,
            argv=argv,
            executable=executable,
            timeout_seconds=long_operation_timeout_seconds(),
            extra_env_names=extra_env_names,
        )
        return self._render(job)

    def _poll(self, job_id: str) -> dict[str, Any]:
        """Return the current (possibly still-running) state of one job."""

        job = self._jobs.get(job_id)
        if job is None:
            raise RelayCliJobError(
                f"unknown relay install job {job_id!r}",
                reason="relay_install_job_not_found",
                details={"job_id": job_id},
            )
        return self._render(job)

    @staticmethod
    def _render(job: RelayInstallJob) -> dict[str, Any]:
        state = effective_job_state(job, idle_seconds=attention_idle_seconds())
        return {**job.to_wire(), "state": state}


def _require_str(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RelayCliJobError(
            f"{key} is required", reason="relay_install_arguments_invalid", details={"field": key}
        )
    return value.strip()


__all__ = [
    "RELAY_INSTALL_NAMESPACE",
    "RELAY_INSTALL_TOOL_NAMES",
    "RelayInstallSurface",
]
