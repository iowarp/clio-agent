"""Curated clio-relay cluster-lifecycle tools (clio-relay#209 A2).

Five tools over the DEPLOYED ``clio-relay`` CLI, run as a local subprocess via
``tools/relay_cli_runner.py``: register a cluster, bootstrap it, compose its status,
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
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations

from clio_agent.tools.relay_cli_runner import (
    RelayCliJobError,
    RelayInstallJob,
    RelayInstallJobRegistry,
    attention_idle_seconds,
    bounded_timeout_seconds,
    effective_job_state,
    long_operation_timeout_seconds,
    resolve_relay_cli_executable,
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

_JOB_HANDLE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "kind": {"type": "string"},
        "state": {
            "type": "string",
            "enum": ["running", "needs_user_attention", "completed", "failed"],
        },
        "terminal": {"type": "boolean"},
        "exit_code": {"type": ["integer", "null"]},
        "receipt_fields": {"type": "array"},
        "parsed_document": {"type": ["object", "null"]},
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

_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "relay_cluster_register": {
        "type": "object",
        "properties": {
            "cluster": _IDENTITY,
            "ssh_host": _IDENTITY,
            "bootstrap_profile": {"type": "string", "default": "linux-user"},
            "core_dir": _OPTIONAL_IDENTITY,
            "spool_dir": _OPTIONAL_IDENTITY,
            "dev_mode": {"type": "boolean", "default": False},
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
        "relay operation -- registers (or updates) a cluster's SSH connection and "
        "deployment profile in clio-relay's local registry. Fast local write; no "
        "SSH connection is made."
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
        "and the same job_id."
    ),
    "relay_proxy_lifecycle": (
        "Use this to install or tear down a cluster's frpc proxy -- action "
        "selects the verb. Both may SSH-dial and return a job handle immediately; "
        "poll with action='status' and the same job_id. A persistent install "
        "refused by the systemd user-lingering gate surfaces as a typed "
        "actionable_refusal naming the 'loginctl enable-linger' remediation, "
        "never a bare failure."
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
                destructive_hint=name in {"relay_proxy_lifecycle", "relay_session_lifecycle"},
                idempotent_hint=name == "relay_cluster_register",
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
            job_registry: Injected job ledger (tests supply a fresh one per case);
                defaults to a process-local instance.
            cli_status: Typed boot-time resolution status for the ``clio-relay``
                executable (``{"configured": bool, "reason": str|None, ...}``),
                retained for diagnostics only -- every tool call re-resolves the
                executable itself rather than trusting a cached boot-time result.
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
        """Dispatch one of the five curated tool names."""

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
        raise ValueError(f"unsupported curated relay install tool: {tool_name!r}")

    # -- register -------------------------------------------------------- #

    async def cluster_register(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Register (or update) a cluster definition. Bounded, no SSH dial."""

        cluster = _require_str(arguments, "cluster")
        ssh_host = _require_str(arguments, "ssh_host")
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
        if bool(arguments.get("dev_mode", False)):
            argv += ["--dev-mode"]
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
        if bool(wheel) != bool(sha):
            raise RelayCliJobError(
                "relay_wheel and relay_artifact_sha256 must be supplied together",
                reason="relay_install_arguments_invalid",
                details={"relay_wheel": bool(wheel), "relay_artifact_sha256": bool(sha)},
            )
        if wheel:
            argv += ["--relay-wheel", str(wheel), "--relay-artifact-sha256", str(sha)]
        if arguments.get("report_path"):
            argv += ["--report", str(arguments["report_path"])]
        if arguments.get("validation_launcher"):
            argv += ["--validation-launcher", str(arguments["validation_launcher"])]
        if arguments.get("validation_install_source"):
            argv += ["--validation-install-source", str(arguments["validation_install_source"])]
        return self._start(kind="relay_cluster_bootstrap", argv=argv)

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
        doctor, info, proxy = await asyncio.gather(
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
        )
        return {
            "cluster": cluster,
            "doctor": doctor.to_wire(),
            "installation_info": info.to_wire(),
            "proxy_status": proxy.to_wire(),
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
                str(int(arguments.get("remote_api_port") or 8765)),
            ]
            argv += _flag_pair(
                bool(arguments.get("replace", False)) or None,
                true_flag="--replace",
                false_flag="--no-replace",
            )
            argv += _flag_pair(
                arguments.get("require_token", True),
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
            return self._start(kind="relay_session_start", argv=argv)
        if action == "attach":
            return self._start(
                kind="relay_session_attach", argv=["session", "attach", "--cluster", cluster]
            )
        if action == "teardown":
            session_id = _require_str(arguments, "session_id")
            argv = ["session", "teardown", "--cluster", cluster, "--session-id", session_id]
            # The exact clio-relay flag shape for a False value is not pinned by the
            # source research this surface was built against (a bool Option whose
            # negative form was not confirmed) -- only assert the flag when True,
            # never guess a "--no-stop-worker" spelling that might not exist.
            if arguments.get("stop_worker"):
                argv += ["--stop-worker"]
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
                str(float(arguments.get("relay_cancel_timeout_seconds") or 30.0)),
                "--relay-cancel-poll-seconds",
                str(float(arguments.get("relay_cancel_poll_seconds") or 0.25)),
            ]
            return self._start(kind="relay_session_teardown", argv=argv)
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
            return self._start(kind="relay_proxy_install", argv=argv)
        if action == "teardown_proxy":
            argv = ["relay-host", "teardown-proxy", "--cluster", cluster]
            if arguments.get("ssh_host"):
                argv += ["--ssh-host", str(arguments["ssh_host"])]
            return self._start(kind="relay_proxy_teardown", argv=argv)
        raise RelayCliJobError(
            f"unsupported relay_proxy_lifecycle action {action!r}",
            reason="relay_install_action_invalid",
            details={"action": action},
        )

    # -- shared job plumbing ------------------------------------------------ #

    def _start(self, *, kind: str, argv: list[str]) -> dict[str, Any]:
        """Spawn one long, SSH-dialing operation and return its handle immediately."""

        executable = resolve_relay_cli_executable()
        job = start_relay_install_job(
            self._jobs,
            kind=kind,
            argv=argv,
            executable=executable,
            timeout_seconds=long_operation_timeout_seconds(),
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
