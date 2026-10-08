"""CLIO Web Search on Apptainer: one owned instance on the host network, loopback only.

The image's own entrypoint cannot run here: it ``chown``s as root and binds the
gateway and Valkey on ``0.0.0.0:8080/6379`` (SearXNG and GROBID on fixed
8888/8070), which on a shared node's host network would expose them and
collide with other users. So, as with Flowcept's Apptainer dependencies:

* the image is digest-pinned and its SIF comes from the shared, digest-keyed
  image store (:func:`~clio_agent.gact.infrastructure.container_runtime.pull_commands`);
  the SIF's sha256 is written to a receipt at installation and every launch
  refuses a SIF that no longer matches it;
* the instance runs with ``instance run --cleanenv --containall`` and a
  CLIO-written launcher bind-mounted over the image's entrypoint (the image's
  ``tini`` still supervises it), which starts each component bound to
  ``127.0.0.1`` at a private port and keeps writes in the owned data folder;
* nothing private reaches argv: the contact email travels as
  ``APPTAINERENV_*`` through the launch command's stdin
  (:mod:`~clio_agent.gact.infrastructure.secret_env`);
* ownership is the instance name plus the SIF path it runs: status, stop and
  cleanup leave an instance of the same name running another image alone;
* readiness is portable: ``/readyz`` answers and ``/v1/capabilities`` names
  this deployment's id (the service has no API key to prove identity with).

Data (``<service>/data``) is not on the ledger: uninstall keeps it for a
reinstall, ``delete_data`` removes it with everything else.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass

from clio_agent.gact.infrastructure.container_runtime import pull_commands
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    OwnedResource,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan, Readiness
from clio_agent.gact.infrastructure.resource_ledger import (
    StepRecorder,
    container_recorder,
    create_directory_command,
    directory_recorder,
    removal_commands,
    shared_image_recorder,
)
from clio_agent.gact.infrastructure.secret_env import with_secret_env
from clio_agent.gact.infrastructure.web_search_service import (
    CONTAINER_NAME,
    EVIDENCE_PATH,
    RUNTIME_FIELD,
    VERIFY_QUERY,
    VERIFY_SCRIPT,
    WEB_SEARCH_PINNED_IMAGE,
    web_search_port,
)

DATA_MOUNT = "/var/lib/clio-web-search"
ENTRYPOINT = "/usr/local/bin/clio-web-search-entrypoint"
GROBID_TMP = "/opt/grobid/grobid-home/tmp"
EMAIL_VARIABLE = "APPTAINERENV_CLIO_WEB_SEARCH_CONTACT_EMAIL"
PRIVATE_PORTS = {
    "task_backend_port": 8090,
    "searxng_port": 18888,
    "grobid_port": 18070,
    "grobid_admin_port": 18071,
}

#: Replaces the image entrypoint inside the instance (runs as the calling user).
#: Mirrors the upstream entrypoint's ordering -- Docling warms before GROBID
#: loads -- with every listener on the loopback at its private port.
LAUNCHER = """#!/bin/sh
# CLIO-managed launcher for CLIO Web Search on Apptainer (replaces the image entrypoint).
set -eu
data=/var/lib/clio-web-search
logs="$data/logs"
mkdir -p "$data/valkey" "$data/home" "$data/tmp" "$logs"
export HOME="$data/home" TMPDIR="$data/tmp" XDG_CACHE_HOME="$data/home/.cache"
port="$CLIO_WS_PORT"
task="$CLIO_WS_TASK_PORT"
searx="$CLIO_WS_SEARXNG_PORT"
grobid="$CLIO_WS_GROBID_PORT"
admin="$CLIO_WS_GROBID_ADMIN_PORT"
export CLIO_WEB_SEARCH_SEARXNG_URL="http://127.0.0.1:$searx"
export CLIO_WEB_SEARCH_GROBID_URL="http://127.0.0.1:$grobid"
export CLIO_WEB_SEARCH_TASK_BACKEND_URL="redis://127.0.0.1:$task/0"
export CLIO_WEB_SEARCH_TASK_BACKEND_PUBLIC_PORT="$task"
settings_path="$(/app/.venv/bin/python -m clio_web_search.configure)"

cleanup() {
    kill -TERM ${gateway_pid:-} ${searxng_pid:-} ${grobid_pid:-} ${valkey_pid:-} 2>/dev/null || true
    wait 2>/dev/null || true
}
shutdown() {
    trap - INT TERM EXIT
    cleanup
    exit 0
}
trap shutdown INT TERM
trap cleanup EXIT

valkey-server /etc/clio-web-search/valkey.conf --bind 127.0.0.1 --port "$task" \\
    --protected-mode yes --dir "$data/valkey" > "$logs/valkey.log" 2>&1 &
valkey_pid=$!

cd /app
./.venv/bin/uvicorn clio_web_search.main:app --host 127.0.0.1 --port "$port" \\
    > "$logs/gateway.log" 2>&1 &
gateway_pid=$!
until curl --fail --silent --noproxy '*' "http://127.0.0.1:$port/healthz" >/dev/null; do
    if ! kill -0 "$gateway_pid" "$valkey_pid" 2>/dev/null; then
        echo "CLIO Web Search failed during Docling startup warmup" >&2
        tail -n 100 "$logs"/*.log >&2 || true
        exit 1
    fi
    sleep 2
done
echo "Docling startup warmup complete" >&2

cd /opt/grobid
JAVA_OPTS="${JAVA_OPTS:-} -Ddw.server.applicationConnectors[0].port=$grobid \\
-Ddw.server.applicationConnectors[0].bindHost=127.0.0.1 \\
-Ddw.server.adminConnectors[0].port=$admin -Ddw.server.adminConnectors[0].bindHost=127.0.0.1" \\
    ./grobid-service/bin/grobid-service > "$logs/grobid.log" 2>&1 &
grobid_pid=$!

cd /opt/searxng
SEARXNG_SETTINGS_PATH="$settings_path" ./.venv/bin/granian searx.webapp:app \\
    --interface wsgi --host 127.0.0.1 --port "$searx" > "$logs/searxng.log" 2>&1 &
searxng_pid=$!

while kill -0 "$gateway_pid" "$searxng_pid" "$grobid_pid" "$valkey_pid" 2>/dev/null; do
    sleep 2
done
echo "A CLIO Web Search child process exited" >&2
tail -n 100 "$logs"/*.log >&2 || true
exit 1
"""

# The owned-instance check shared by status and stop: $0 is the instance name,
# $1 the deployment's SIF path. Apptainer may list the image by the symlink or
# by the store file it resolves to; any other image is another deployment's.
_OWNED_ROW = (
    'row=$(apptainer instance list "$0" 2>/dev/null | awk \'NR==2{print $2" "$NF}\'); '
    'pid=${row%% *}; img=${row#* }; real=$(readlink -f "$1" 2>/dev/null || printf %s "$1"); '
    'if [ -z "$row" ]; then state=stopped; '
    'elif [ "$img" != "$1" ] && [ "$img" != "$real" ]; then state=foreign_instance; '
    "else state=owned; fi; "
)


@dataclass(frozen=True)
class Layout:
    """Where one Apptainer web-search deployment keeps everything on its host."""

    service_dir: str
    data_dir: str
    runtime_dir: str
    grobid_tmp: str
    launcher: str
    images_dir: str
    sif: str
    receipt: str
    temporary: str
    image_store: str
    deployment_id: str


def _layout(
    facts: TargetFacts, target: InfrastructureTarget | None, configuration: dict[str, str]
) -> Layout:
    if facts.os != "linux":
        raise ValueError("Apptainer runs CLIO Web Search on Linux hosts only")
    host = facts.hostname or facts.target_id
    service_dir = configuration.get("storage.service_directory", "")
    temporary = configuration.get("storage.temporary", "")
    image_store = configuration.get("storage.image_store", "")
    if target and any(target.storage.model_dump().values()):
        from clio_agent.gact.infrastructure.storage import resolved_locations  # noqa: PLC0415

        locations = resolved_locations(target, facts)
        service_dir = service_dir or posixpath.join(locations.service_data, host, CONTAINER_NAME)
        temporary = temporary or posixpath.join(locations.temporary, host, CONTAINER_NAME)
        image_store = image_store or posixpath.join(locations.service_data, "apptainer-images")
    if not service_dir:
        root = (target.install_root.strip() if target else "").rstrip("/") or facts.agent_data_root
        if not root and facts.home:
            root = posixpath.join(facts.home, ".local", "share", "clio-agent")
        if not root:
            raise ValueError(
                "Could not determine the target's home directory; set an install location."
            )
        # Per host: cluster nodes share one home (see service_paths.service_directory).
        service_dir = posixpath.join(root, "services", host, CONTAINER_NAME)
    if not posixpath.isabs(service_dir) or any(ch in service_dir for ch in ":,\n\r\0"):
        raise ValueError("The web search folder must be an absolute path without ':' or ','")
    service_dir = posixpath.normpath(service_dir)
    temporary = temporary or posixpath.join(service_dir, "tmp")
    image_store = image_store or posixpath.join(posixpath.dirname(service_dir), "apptainer-images")
    images_dir = posixpath.join(service_dir, "images")
    runtime_dir = posixpath.join(service_dir, "runtime")
    sif = posixpath.join(images_dir, f"{CONTAINER_NAME}.sif")
    ownership = hashlib.sha256(f"{facts.target_id}:{host}:{service_dir}".encode()).hexdigest()
    # The installed id is kept for the deployment's life; a new one is derived.
    deployment_id = configuration.get("deployment_id", "") or f"clio-ws-{ownership[:12]}"
    if not re.fullmatch(r"clio-ws-[0-9a-f]{12}", deployment_id):
        raise ValueError("The web search deployment id is not one CLIO issued")
    return Layout(
        service_dir=service_dir,
        data_dir=posixpath.join(service_dir, "data"),
        runtime_dir=runtime_dir,
        grobid_tmp=posixpath.join(runtime_dir, "grobid-tmp"),
        launcher=posixpath.join(runtime_dir, "entrypoint.sh"),
        images_dir=images_dir,
        sif=sif,
        receipt=sif + ".sha256",
        temporary=temporary,
        image_store=image_store,
        deployment_id=deployment_id,
    )


def _ports(configuration: dict[str, str]) -> dict[str, int]:
    ports = {"port": web_search_port({**configuration, RUNTIME_FIELD: "apptainer"})}
    for key, default in PRIVATE_PORTS.items():
        value = configuration.get(key, "").strip() or str(default)
        if not value.isdigit() or not 1024 <= int(value) <= 65535:
            raise ValueError(f"{key} must be a number from 1024 to 65535")
        ports[key] = int(value)
    if len(set(ports.values())) != len(ports):
        raise ValueError("The web search gateway and its private services need distinct ports")
    return ports


def status_command(layout: Layout, *, require: bool = False) -> CommandSpec:
    """``running``/``exited``/``stopped``/``foreign_instance`` for the owned instance.

    An instance outlives the launcher it ran (F034): it is running only while
    the instance process still has a child. ``require`` fails unless running.
    """

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            _OWNED_ROW + 'if [ "$state" = owned ]; then '
            "if ! command -v pgrep >/dev/null 2>&1; then state=running; "
            'elif pgrep -P "$pid" >/dev/null 2>&1; then state=running; '
            "else state=exited; fi; fi; "
            'echo "$state"; '
            'if [ "$2" = require ] && [ "$state" != running ]; then '
            'echo "CLIO Web Search is not serving on this host ($state)" >&2; exit 3; fi',
            CONTAINER_NAME,
            layout.sif,
            "require" if require else "observe",
        ],
    )


def stop_owned_command(layout: Layout) -> CommandSpec:
    """Stop the owned instance if present; refuse an instance of the name that is not ours."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            _OWNED_ROW + 'if [ "$state" = foreign_instance ]; then '
            'echo "foreign_instance: an Apptainer instance named $0 runs $img, not this '
            'deployment; CLIO leaves it alone" >&2; exit 3; fi; '
            'if [ "$state" = owned ]; then apptainer instance stop "$0" >/dev/null 2>&1; '
            'if apptainer instance list "$0" 2>/dev/null | tail -n +2 | grep -q .; then '
            'echo "still present: $0" >&2; exit 1; fi; fi; echo stopped',
            CONTAINER_NAME,
            layout.sif,
        ],
        timeout_seconds=120,
    )


def logs_command(layout: Layout, lines: int = 80) -> CommandSpec:
    """The instance's own output and each component's log in the data folder."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'for f in "$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".out '
            '"$HOME"/.apptainer/instances/logs/*/"$USER"/"$0".err "$1"/logs/*.log; do '
            '[ -f "$f" ] && { echo "== ${f##*/}"; tail -n "$2" "$f"; }; done; true',
            CONTAINER_NAME,
            layout.data_dir,
            str(lines),
        ],
    )


def receipt_command(layout: Layout) -> CommandSpec:
    """Record the installed SIF's sha256 (its immutable identity) beside it."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'real=$(readlink -f "$0") && sum=$(sha256sum "$real" | cut -d " " -f 1) && '
            '[ -n "$sum" ] && printf "%s\\n" "$sum" > "$1.tmp" && mv -f "$1.tmp" "$1" && '
            'echo "CLIO_IMAGE_SHA256 $sum"',
            layout.sif,
            layout.receipt,
        ],
        timeout_seconds=1800,
    )


def write_launcher_command(layout: Layout) -> CommandSpec:
    """Write the launcher that is bind-mounted over the image entrypoint."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'umask 022; printf "%s" "$1" > "$0.tmp" && chmod 0755 "$0.tmp" && mv -f "$0.tmp" "$0"',
            layout.launcher,
            LAUNCHER,
        ],
    )


def run_command(layout: Layout, ports: dict[str, int], email: str) -> CommandSpec:
    """Start the instance from the receipt-verified SIF; private values via the environment."""

    launch = [
        "apptainer",
        "instance",
        "run",
        "--cleanenv",
        "--containall",
        "--writable-tmpfs",
        "--bind",
        f"{layout.data_dir}:{DATA_MOUNT}",
        "--bind",
        f"{layout.grobid_tmp}:{GROBID_TMP}",
        "--bind",
        f"{layout.launcher}:{ENTRYPOINT}:ro",
        "--env",
        f"CLIO_WS_PORT={ports['port']}",
        "--env",
        f"CLIO_WS_TASK_PORT={ports['task_backend_port']}",
        "--env",
        f"CLIO_WS_SEARXNG_PORT={ports['searxng_port']}",
        "--env",
        f"CLIO_WS_GROBID_PORT={ports['grobid_port']}",
        "--env",
        f"CLIO_WS_GROBID_ADMIN_PORT={ports['grobid_admin_port']}",
        "--env",
        f"CLIO_WEB_SEARCH_DEPLOYMENT_ID={layout.deployment_id}",
        layout.sif,
        CONTAINER_NAME,
    ]
    command = CommandSpec(
        program="sh",
        args=[
            "-c",
            'real=$(readlink -f "$0") || exit 65; want=$(cat "$1" 2>/dev/null || true); '
            'sum=$(sha256sum "$real" | cut -d " " -f 1); '
            'if [ -z "$want" ] || [ "$sum" != "$want" ]; then '
            'echo "image_receipt_mismatch: the installed web search image no longer matches '
            'its receipt; reinstall it" >&2; exit 65; fi; shift; exec "$@"',
            layout.sif,
            layout.receipt,
            *launch,
        ],
        timeout_seconds=1800,
    )
    if not email:
        return command
    if "@" not in email or any(character.isspace() for character in email):
        raise ValueError("contact_email must be a valid email address")
    return with_secret_env(command, EMAIL_VARIABLE, email, windows=False)


def identity_command(layout: Layout, port: int) -> CommandSpec:
    """Ready only when the gateway is ready AND reports this deployment's id."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            "command -v curl >/dev/null 2>&1 || { echo no_http_client; exit 0; }; "
            'curl -fsS -m 5 -o /dev/null --noproxy "*" "$0/readyz" 2>/dev/null '
            "|| { echo waiting; exit 0; }; "
            'body=$(curl -fsS -m 5 --noproxy "*" "$0/v1/capabilities" 2>/dev/null) '
            "|| { echo waiting; exit 0; }; "
            'case "$body" in *\'"service":"clio-web-search"\'*) ;; '
            "*) echo foreign_endpoint; exit 0 ;; esac; "
            'case "$body" in *"\\"deployment_id\\":\\"$1\\""*) echo ready ;; '
            "*) echo foreign_endpoint ;; esac",
            f"http://127.0.0.1:{port}",
            layout.deployment_id,
        ],
        timeout_seconds=30,
    )


def _data_recorder(layout: Layout) -> StepRecorder:
    """Record the data folder's new parents, never the folder itself (uninstall keeps it)."""

    record = directory_recorder(layout.data_dir)

    def keep_parents(result: CommandResult) -> list[OwnedResource]:
        return [row for row in record(result) if row.kind == "parent_directory"]

    return keep_parents


def _delete_data_command(layout: Layout) -> CommandSpec:
    return CommandSpec(
        program="sh",
        args=[
            "-c",
            'rm -rf -- "$0"; if [ -e "$0" ]; then echo "still present: $0"; exit 1; fi',
            layout.data_dir,
        ],
        timeout_seconds=600,
    )


def apptainer_web_search_plan(
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    owned: list[OwnedResource],
) -> DriverPlan:
    """Compile one lifecycle action for the Apptainer deployment."""

    layout = _layout(facts, target, configuration)
    ports = _ports(configuration)
    resolved = {
        **configuration,
        RUNTIME_FIELD: "apptainer",
        "port": str(ports["port"]),
        **{key: str(ports[key]) for key in PRIVATE_PORTS},
        "storage.service_directory": layout.service_dir,
        "storage.temporary": layout.temporary,
        "storage.image_store": layout.image_store,
        "deployment_id": layout.deployment_id,
    }
    port = ports["port"]

    def plan(
        commands: list[CommandSpec],
        recorders: dict[int, StepRecorder] | None = None,
        readiness: Readiness | None = None,
    ) -> DriverPlan:
        return DriverPlan(
            tuple(commands),
            connection_port=port,
            configuration=resolved,
            recorders=recorders or {},
            readiness=readiness,
        )

    if action == "status":
        return plan([status_command(layout)])
    if action == "logs":
        return plan([logs_command(layout)])
    if action == "stop":
        return plan([stop_owned_command(layout)])
    if action == "verify":
        return plan(
            [
                status_command(layout, require=True),
                CommandSpec(
                    program="apptainer",
                    args=[
                        "exec",
                        "--cleanenv",
                        f"instance://{CONTAINER_NAME}",
                        "/app/.venv/bin/python",
                        "-c",
                        VERIFY_SCRIPT,
                        f"http://127.0.0.1:{port}",
                        EVIDENCE_PATH,
                        VERIFY_QUERY,
                    ],
                    timeout_seconds=180,
                ),
            ]
        )
    if action in {"uninstall", "delete_data"}:
        # Scoped stop first: the ledger's by-name removal must never reach an
        # instance of the same name that runs another deployment's image.
        # delete_data empties the data folder before the ledger's only-if-empty
        # parent removal, so the deployment's folders go with it.
        commands = [stop_owned_command(layout)]
        if action == "delete_data":
            commands.append(_delete_data_command(layout))
        return plan([*commands, *removal_commands(owned, facts.os)])
    readiness = Readiness(
        health=identity_command(layout, port),
        alive=status_command(layout),
        logs=logs_command(layout, lines=40),
        label="CLIO Web Search",
    )
    email = configuration.get("contact_email", "").strip()
    if action == "start":
        return plan(
            [stop_owned_command(layout), run_command(layout, ports, email)],
            recorders={1: container_recorder("apptainer", CONTAINER_NAME, facts.hostname)},
            readiness=readiness,
        )
    if action not in {"install", "reinstall"}:
        raise ValueError(f"Unsupported lifecycle action {action!r}")
    commands = [stop_owned_command(layout), _ports_free_command(ports)]
    recorders: dict[int, StepRecorder] = {}
    recorders[len(commands)] = _data_recorder(layout)
    commands.append(create_directory_command(layout.data_dir, facts.os))
    for directory in (layout.runtime_dir, layout.grobid_tmp, layout.images_dir, layout.temporary):
        recorders[len(commands)] = directory_recorder(directory, facts.os)
        commands.append(create_directory_command(directory, facts.os))
    commands.append(write_launcher_command(layout))
    commands.extend(
        pull_commands(
            "apptainer",
            WEB_SEARCH_PINNED_IMAGE,
            layout.images_dir,
            layout.image_store,
            CONTAINER_NAME,
            posixpath.join(layout.temporary, "apptainer-tmp"),
        )
    )
    recorders[len(commands) - 1] = shared_image_recorder()
    commands.append(receipt_command(layout))
    recorders[len(commands)] = container_recorder("apptainer", CONTAINER_NAME, facts.hostname)
    commands.append(run_command(layout, ports, email))
    return plan(commands, recorders=recorders, readiness=readiness)


def _ports_free_command(ports: dict[str, int]) -> CommandSpec:
    """Refuse a port another process already holds (the host network is shared)."""

    return CommandSpec(
        program="sh",
        args=[
            "-c",
            "if ! command -v ss >/dev/null 2>&1; then "
            'echo "port_check_unavailable: ss is not installed; ports were not checked"; '
            "exit 0; fi; "
            'for p in "$@"; do if [ -n "$(ss -Hltn "sport = :$p" 2>/dev/null)" ]; then '
            'echo "port_in_use: port $p on this host is already in use by another process; '
            'choose another port for web search."; exit 3; fi; done; true',
            "clio-web-search-ports",
            *(str(value) for value in ports.values()),
        ],
    )
