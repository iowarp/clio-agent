"""CLIO Web Search: its catalog row and lifecycle plans on Docker, Podman or Apptainer.

The OCI image (``ghcr.io/iowarp/clio-web-search``) runs a gateway, SearXNG and
Valkey; its full variant adds document conversion (Docling + GROBID), its slim
variant is search only. The ``documents`` install option picks the variant
(:func:`web_search_image`). On Docker and Podman the image's own entrypoint runs it
with published ports (unchanged from the Docker-only driver). On Apptainer
there is no port publishing, so the deployment is the digest-pinned image's
SIF run as a user instance on the host network with every listener bound to
the loopback at a private port (:mod:`~clio_agent.gact.infrastructure.web_search_apptainer`).
"""

from __future__ import annotations

import ipaddress
import ntpath
import posixpath
from dataclasses import dataclass

from clio_agent.gact.infrastructure import powershell
from clio_agent.gact.infrastructure.container_runtime import (
    RuntimeUnavailableError,
    negotiate_runtime,
    parse_runtime_name,
    usable_runtimes,
)
from clio_agent.gact.infrastructure.model_runtimes import _runtimes as probed_runtimes
from clio_agent.gact.infrastructure.models import (
    RUNTIME_LABELS,
    CommandSpec,
    InfrastructureTarget,
    ManagedServiceDefinition,
    OwnedResource,
    RuntimeName,
    ServiceConfigurationField,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan

WEB_SEARCH_VERSION = "0.3.2"
#: Docker and Podman run the release tag (the Docker-only driver's behaviour).
WEB_SEARCH_IMAGE = f"ghcr.io/iowarp/clio-web-search:{WEB_SEARCH_VERSION}"
#: Apptainer runs only a digest-pinned image (its SIF is keyed by this digest).
#: ghcr.io/iowarp/clio-web-search:0.3.2 (OCI image index, amd64+arm64, ~1.8 GB
#: compressed), resolved 2026-10-08 from the registry. Re-check after a tag change:
#:   crane digest ghcr.io/iowarp/clio-web-search:0.3.2
#:   skopeo inspect --format '{{.Digest}}' docker://ghcr.io/iowarp/clio-web-search:0.3.2
WEB_SEARCH_PINNED_IMAGE = (
    "ghcr.io/iowarp/clio-web-search@sha256:"
    "c2cb347b035410230105f10252db0251f7d29c9766d0637ba802a8d8ac2a8994"
)
#: The slim, search-only image (gateway + SearXNG + Valkey; no Docling, GROBID or
#: models; ~0.11 GB compressed): the default install when ``documents`` is off.
#: ghcr.io/iowarp/clio-web-search:0.3.2-slim (OCI image index, amd64+arm64),
#: resolved 2026-10-08. Re-check after a tag change with:
#:   crane digest ghcr.io/iowarp/clio-web-search:0.3.2-slim
#: and paste only the 64 hex characters after ``sha256:``.
WEB_SEARCH_SLIM_DIGEST = "f0bb4a7d03e879d4e19e8b2271993b5dd478165e10133f8c224425f457a5fd75"
WEB_SEARCH_REPOSITORY = "ghcr.io/iowarp/clio-web-search"
DOCUMENTS_FIELD = "documents"
#: Recorded at install: which image variant the deployment runs (``full``/``slim``).
IMAGE_VARIANT_FIELD = "image_variant"
WEB_SEARCH_PORT = 8089
LISTEN_FIELD = "listen_address"
CONTAINER_NAME = "clio-web-search"
RUNTIME_FIELD = "container_runtime"
#: Where the verification probe leaves its evidence inside the container.
EVIDENCE_PATH = "/var/lib/clio-web-search/evidence/verification.json"
#: One real search through the gateway; at least one result with a URL passes.
VERIFY_QUERY = "HDF5 hierarchical data format"
VERIFY_SCRIPT = """import json, os, sys, time, urllib.parse, urllib.request, uuid
url, out, query = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(out), exist_ok=True)
def write(payload):
    with open(out + ".tmp", "w") as stream:
        json.dump(payload, stream)
    os.replace(out + ".tmp", out)
write({})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
search = url + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
with opener.open(search, timeout=90) as response:
    body = json.load(response)
results = [row for row in body.get("results") or [] if row.get("url")]
if not results:
    engines = [row[0] for row in body.get("unresponsive_engines") or [] if row][:10]
    sys.stderr.write("web search returned no results; unresponsive engines: %s\\n" % engines)
    sys.exit(4)
evidence = {
    "service": "web_search",
    "verification_id": str(uuid.uuid4()),
    "query": query,
    "result_count": len(results),
    "first_result_url": results[0]["url"],
    "search_ok": True,
    "verified_at": time.time(),
}
write(evidence)
print(json.dumps(evidence))
"""


@dataclass(frozen=True)
class WebSearchImage:
    """The image one deployment runs and whether it carries document conversion."""

    variant: str
    #: Docker and Podman reference (the release tag for the full image).
    reference: str
    #: Apptainer reference (always digest-pinned).
    pinned: str
    documents: bool


FULL_IMAGE = WebSearchImage("full", WEB_SEARCH_IMAGE, WEB_SEARCH_PINNED_IMAGE, documents=True)


def slim_image() -> WebSearchImage | None:
    """The published slim image, or ``None`` while its digest is not pinned here."""

    digest = WEB_SEARCH_SLIM_DIGEST.strip().removeprefix("sha256:")
    if not digest:
        return None
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("WEB_SEARCH_SLIM_DIGEST must be a sha256 hex digest")
    reference = f"{WEB_SEARCH_REPOSITORY}@sha256:{digest}"
    return WebSearchImage("slim", reference, reference, documents=False)


def documents_requested(configuration: dict[str, str]) -> bool:
    """The ``documents`` install option: off (search only) unless the person turned it on."""

    raw = configuration.get(DOCUMENTS_FIELD, "").strip().lower() or "off"
    if raw in {"on", "true", "yes", "1"}:
        return True
    if raw in {"off", "false", "no", "0"}:
        return False
    raise ValueError("documents must be 'on' or 'off'")


def web_search_image(configuration: dict[str, str]) -> WebSearchImage:
    """The image an install uses: slim when documents are off and the slim digest is pinned."""

    slim = slim_image()
    if documents_requested(configuration) or slim is None:
        return FULL_IMAGE
    return slim


def installed_image(configuration: dict[str, str]) -> WebSearchImage:
    """The image an existing deployment runs (recorded at install; older ones are full)."""

    if configuration.get(IMAGE_VARIANT_FIELD, "").strip() == "slim":
        slim = slim_image()
        if slim is not None:
            return slim
    return FULL_IMAGE


def _field(
    field_id: str, label: str, placeholder: str, options: list[str] | None = None
) -> ServiceConfigurationField:
    return ServiceConfigurationField(
        id=field_id, label=label, placeholder=placeholder, options=options or []
    )


def web_search_port(configuration: dict[str, str] | None = None) -> int:
    """The gateway port: configurable on Apptainer (host network), fixed when published."""

    configuration = configuration or {}
    if configuration.get(RUNTIME_FIELD) != "apptainer":
        return WEB_SEARCH_PORT
    value = configuration.get("port", "").strip() or str(WEB_SEARCH_PORT)
    if not value.isdigit() or not 1024 <= int(value) <= 65535:
        raise ValueError("port must be a number from 1024 to 65535")
    return int(value)


def web_search_definition(facts: TargetFacts) -> ManagedServiceDefinition:
    """The catalog row: offered when Docker, Podman or Apptainer is usable here."""

    runtimes = probed_runtimes(facts)
    options = usable_runtimes(runtimes)
    try:
        chosen = negotiate_runtime(runtimes)
        compatible, reason = True, f"Runs with {RUNTIME_LABELS[chosen.name]}."
    except RuntimeUnavailableError as exc:
        compatible, reason = False, str(exc)
    default = web_search_image({})
    return ManagedServiceDefinition(
        id="web_search",
        category="scientific_service",
        label="CLIO Web Search",
        description="Private web search; PDF/document conversion is optional.",
        recommended_variant="container",
        variants=[
            ServiceVariant(
                id="container",
                label="Container",
                version=WEB_SEARCH_VERSION,
                install_type="container",
                artifact=default.pinned if options[:1] == ["apptainer"] else default.reference,
                compatible=compatible,
                reason=reason,
            )
        ],
        configuration_fields=[
            _field(
                DOCUMENTS_FIELD,
                "Document conversion",
                "off (on adds PDF/document conversion; larger, slower install)",
                ["off", "on"],
            ),
            _field("contact_email", "Publication metadata email", "scientist@example.org"),
            _field("task_backend_port", "Document task port", "8090"),
            _field(LISTEN_FIELD, "Listen address", "127.0.0.1 (0.0.0.0 serves every network)"),
            _field(
                RUNTIME_FIELD,
                "Container runtime",
                f"Automatic ({RUNTIME_LABELS[options[0]]})" if options else "No usable runtime",
                list(options),
            ),
            _field("port", "Search port (Apptainer)", str(WEB_SEARCH_PORT)),
            _field("searxng_port", "Private SearXNG port (Apptainer)", "18888"),
            _field("grobid_port", "Private GROBID port (Apptainer)", "18070"),
            _field("grobid_admin_port", "Private GROBID admin port (Apptainer)", "18071"),
        ],
    )


def _runtime(action: str, configuration: dict[str, str], facts: TargetFacts) -> RuntimeName:
    """The installing runtime is negotiated; later actions use the installed one.

    A deployment recorded before the runtime was negotiated is a Docker one.
    """

    chosen = configuration.get(RUNTIME_FIELD, "").strip()
    if action in {"install", "reinstall"}:
        return negotiate_runtime(probed_runtimes(facts), chosen).name
    return parse_runtime_name(chosen or "docker")


def build_web_search_plan(
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    owned: list[OwnedResource] | None,
) -> DriverPlan:
    """Compile one lifecycle action in the deployment's container runtime."""

    runtime = _runtime(action, configuration, facts)
    if runtime == "apptainer":
        from clio_agent.gact.infrastructure.web_search_apptainer import (  # noqa: PLC0415
            apptainer_web_search_plan,
        )

        return apptainer_web_search_plan(action, configuration, facts, target, owned or [])
    image = (
        web_search_image(configuration)
        if action in {"install", "reinstall"}
        else installed_image(configuration)
    )
    resolved = {
        **configuration,
        RUNTIME_FIELD: runtime,
        DOCUMENTS_FIELD: "on" if documents_requested(configuration) else "off",
        IMAGE_VARIANT_FIELD: image.variant,
    }
    if action == "verify":
        return DriverPlan(
            (
                CommandSpec(
                    program=runtime,
                    args=[
                        "exec",
                        "--user",
                        "65534:65534",
                        CONTAINER_NAME,
                        "/app/.venv/bin/python",
                        "-c",
                        VERIFY_SCRIPT,
                        "http://127.0.0.1:8080",
                        EVIDENCE_PATH,
                        VERIFY_QUERY,
                    ],
                    timeout_seconds=180,
                ),
            ),
            connection_port=WEB_SEARCH_PORT,
            configuration=resolved,
        )
    if action == "delete_data":
        raise ValueError(
            "Separate deletion of web search data is available on Apptainer; on "
            f"{RUNTIME_LABELS[runtime]} remove the service's data volume with the engine"
        )
    if action in {"status", "logs", "stop", "uninstall"}:
        return _container_lifecycle(runtime, action)
    if action == "start":
        return DriverPlan(
            (CommandSpec(program=runtime, args=["start", CONTAINER_NAME]),),
            connection_port=WEB_SEARCH_PORT,
            configuration=resolved,
        )
    commands: list[CommandSpec] = []
    if action == "reinstall":
        commands.append(
            CommandSpec(
                program=runtime,
                args=["rm", "--force", CONTAINER_NAME],
                allowed_exit_codes=[0, 1],
                settle_seconds=1.0,
            )
        )
    commands.append(
        CommandSpec(program=runtime, args=["pull", image.reference], timeout_seconds=900)
    )
    storage = _container_storage_path(target, facts)
    if storage:
        commands.append(_create_directory(storage, facts))
    commands.append(_container_run(runtime, configuration, facts, storage, image.reference))
    return DriverPlan(tuple(commands), connection_port=WEB_SEARCH_PORT, configuration=resolved)


def _container_lifecycle(runtime: RuntimeName, action: str) -> DriverPlan:
    container = CONTAINER_NAME
    if action == "status":
        return DriverPlan(
            (
                CommandSpec(
                    program=runtime, args=["inspect", "--format", "{{.State.Status}}", container]
                ),
            )
        )
    if action == "logs":
        return DriverPlan((CommandSpec(program=runtime, args=["logs", "--tail", "80", container]),))
    if action == "stop":
        return DriverPlan((CommandSpec(program=runtime, args=["stop", container]),))
    return DriverPlan(
        (
            CommandSpec(
                program=runtime,
                args=["rm", "--force", container],
                allowed_exit_codes=[0, 1],
                settle_seconds=1.0,
            ),
        )
    )


def _container_run(
    runtime: RuntimeName,
    configuration: dict[str, str],
    facts: TargetFacts,
    storage: str | None,
    image: str,
) -> CommandSpec:
    bind = _listen_address(configuration)
    email = configuration.get("contact_email", "").strip()
    task_port = configuration.get("task_backend_port", "8090").strip() or "8090"
    if not task_port.isdigit() or not 1 <= int(task_port) <= 65535:
        raise ValueError("task_backend_port must be a valid port")
    args = [
        "run",
        "--detach",
        "--name",
        CONTAINER_NAME,
        "--restart",
        "unless-stopped",
        "--publish",
        f"{bind}:{WEB_SEARCH_PORT}:8080",
        "--publish",
        f"{bind}:{task_port}:6379",
        "--volume",
        f"{storage or 'clio-web-search-data'}:/var/lib/clio-web-search",
        "--env",
        f"CLIO_WEB_SEARCH_TASK_BACKEND_PUBLIC_PORT={task_port}",
    ]
    if email:
        if "@" not in email or any(character.isspace() for character in email):
            raise ValueError("contact_email must be a valid email address")
        args.extend(["--env", f"CLIO_WEB_SEARCH_CONTACT_EMAIL={email}"])
    args.append(image)
    return CommandSpec(program=runtime, args=args)


def _listen_address(configuration: dict[str, str]) -> str:
    """The published-port address: loopback unless the person chose another (F001).

    The search API and its task backend have no authentication, so they are not
    published on every interface of a shared host by default; a remote host is
    reached through an SSH forward instead.
    """

    raw = configuration.get(LISTEN_FIELD, "").strip() or "127.0.0.1"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ValueError(f"{LISTEN_FIELD} must be an IP address such as 127.0.0.1") from exc
    return f"[{address}]" if address.version == 6 else str(address)


def _container_storage_path(target: InfrastructureTarget | None, facts: TargetFacts) -> str | None:
    """Return an optional service-owned bind directory under the configured root."""

    if target is None or not target.install_root.strip():
        return None
    root = target.install_root.strip().rstrip("/\\")
    path_module = ntpath if facts.os == "windows" else posixpath
    return path_module.join(root, "services", "web-search")


def _create_directory(path: str, facts: TargetFacts) -> CommandSpec:
    """Create one validated driver-owned directory without invoking a shell on Linux."""

    if facts.os == "windows":
        # -Command never binds trailing arguments to $args: embed a literal.
        return powershell.command(
            f"New-Item -ItemType Directory -Force -Path {powershell.literal(path)} | Out-Null"
        )
    return CommandSpec(program="mkdir", args=["-p", "--", path])
