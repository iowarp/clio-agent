"""The managed model router: one upstream LiteLLM Proxy in front of a target's vLLM instances.

CLIO does no routing of its own. It installs a pinned LiteLLM Proxy into its
own uv environment (the native supervisor of
:mod:`~clio_agent.gact.infrastructure.supervised_service`, the same pattern as
native vLLM and the monitoring services -- never CLIO's environment), generates
its configuration from the running instances, and supervises it.

Why LiteLLM Proxy (not a home-grown router, and vllm-router only as fallback):

* CLIO's model library already is LiteLLM; the proxy is pinned to CLIO's own
  ``litellm`` version (:data:`LITELLM_VERSION`), so both ends speak the same
  wire semantics.
* It is pure Python, installable with uv on Linux, macOS and Windows (the
  CLIO supervisor that runs it is POSIX today), and its whole dependency set
  is locked in the service's ``uv.lock``.
* ``hosted_vllm`` passes streaming, tool calls and ``reasoning_content``
  through, and forwards vLLM-specific request fields (``chat_template_kwargs``,
  ``thinking_token_budget``) to the instance in ``extra_body``.
* It authenticates with a master key (``Authorization: Bearer``), refusing
  keyless requests, and holds one upstream key per instance.

vLLM's own ``vllm-router`` is the fallback should a live requirement fail
(it is Rust, Linux-first, and routes one model's replicas rather than many
models).

The generated configuration names every secret by environment reference
(``os.environ/...``); the master key and the instances' keys reach the proxy
only through its environment (a launch's stdin, never arguments, the manifest
or logs). The model list is a per-launch file, so it changes without a
reinstall: a start, stop or removal of an instance restarts the proxy with the
new list (LiteLLM hot-reloads models only from a database, which CLIO does
not give it). Readiness proves identity: a keyless ``/v1/models`` is refused
and the keyed one lists every configured model.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from clio_agent.gact.infrastructure.model_instances import (
    PORT_FIELD,
    ROUTER_SERVICE,
    engine_of,
    instance_name,
    instances_on,
)
from clio_agent.gact.infrastructure.model_runtimes import service_port
from clio_agent.gact.infrastructure.models import (
    InfrastructureTarget,
    ManagedServiceDefinition,
    ServiceConfigurationField,
    ServiceRecord,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.native_vllm import RUN_GUARD, served_model
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.server_access import (
    KEY_VARIABLES,
    is_shareable,
    supports_api_key,
)
from clio_agent.gact.infrastructure.server_parameters import compile_parameters
from clio_agent.gact.infrastructure.service_paths import service_directory
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.supervised_service import supervised_plan

#: Kept equal to CLIO's own ``litellm`` pin (pyproject.toml).
LITELLM_VERSION = "1.102.1"
ROUTER_VARIANT = "native-litellm"
DEFINITION_VERSION = f"litellm-proxy-{LITELLM_VERSION}-1"
CONTAINER_NAME = "clio-model-router"
HEALTH_PATH = "/health/liveliness"
CONFIG_FILE = "router-config.yaml"
MODELS_FILE = "router-models.json"
#: Settled configuration: the routed models (JSON ``{model: instance}``), the
#: digest of the configuration in force, and whether CLIO stopped the router
#: because no instance was left to route to (it starts again with the next one).
MODELS_FIELD = "router.models"
DIGEST_FIELD = "router.config_sha256"
IDLE_FIELD = "router.idle"
MASTER_KEY_VARIABLE = KEY_VARIABLES[ROUTER_SERVICE]
UPSTREAM_KEY_PREFIX = "CLIO_ROUTER_UPSTREAM_KEY_"
#: A keyless (shareable) instance ignores the header; LiteLLM needs some value.
_NO_UPSTREAM_KEY = "no-key"

LAUNCHER = (
    "import sys\n"
    + RUN_GUARD
    + "    from litellm import run_server\n"
    + "    sys.argv[0] = 'litellm'\n"
    + "    run_server()\n"
)

#: No telemetry, no database, no cost-map download, no .env from the cwd, and
#: upstream calls to the loopback never through a site HTTP proxy.
PROXY_ENVIRONMENT = {
    "LITELLM_TELEMETRY": "False",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "LITELLM_MODE": "PRODUCTION",
    "DATABASE_URL": "",
    "STORE_MODEL_IN_DB": "False",
    "DISABLE_ADMIN_UI": "True",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}


@dataclass(frozen=True)
class RoutedModel:
    """One model the router serves: a running instance's served model."""

    name: str
    service_id: str
    served: str
    port: int
    keyed: bool


@dataclass(frozen=True)
class RouterInputs:
    """What a router launch needs from the store: its models and their instances' keys."""

    models: tuple[RoutedModel, ...] = ()
    upstream_keys: tuple[tuple[str, str], ...] = ()


def router_definition(facts: TargetFacts) -> ManagedServiceDefinition:
    """The catalog row of the model router on the inspected target."""

    compatible = facts.os in {"linux", "macos"} and facts.uv_available
    return ManagedServiceDefinition(
        id=ROUTER_SERVICE,
        category="model_runtime",
        label="Model router",
        description=("One address and key for every vLLM instance on this host (LiteLLM Proxy)."),
        recommended_variant=ROUTER_VARIANT,
        variants=[
            ServiceVariant(
                id=ROUTER_VARIANT,
                label="LiteLLM Proxy (native)",
                version=LITELLM_VERSION,
                install_type="native_uv",
                artifact=f"litellm[proxy]=={LITELLM_VERSION}",
                compatible=compatible,
                reason=(
                    "Ready for native installation."
                    if compatible
                    else "Requires a Linux or macOS host with uv."
                ),
            )
        ],
        configuration_fields=[
            ServiceConfigurationField(id=PORT_FIELD, label="Port", placeholder="Automatic")
        ],
        supports_api_key=True,
    )


def instance_served_model(record: ServiceRecord) -> str:
    """The model id a vLLM deployment lists (``--served-model-name``, else its model)."""

    configuration = record.configuration
    variant = "cuda" if record.variant_id.startswith("native") else record.variant_id
    try:
        flags = compile_parameters("vllm", variant, configuration).flags
    except ValueError:
        flags = []
    return served_model(configuration.get("model", "").strip(), flags)


def model_names(records: Iterable[ServiceRecord], target_id: str) -> dict[str, ServiceRecord]:
    """The name the router serves each vLLM deployment on ``target_id`` under.

    A deployment's own served model id; when two deployments serve the same
    id, the later instance's is ``<model>@<instance>``. Named over every
    deployment (running or not), so a name does not move when one stops.
    """

    names: dict[str, ServiceRecord] = {}
    for record in instances_on(records, target_id, "vllm"):
        served = instance_served_model(record)
        if not served:
            continue
        suffix = instance_name(record.service_id) or record.service_id
        names[served if served not in names else f"{served}@{suffix}"] = record
    return names


def routed_models(records: Iterable[ServiceRecord], target_id: str) -> tuple[RoutedModel, ...]:
    """The models of the running vLLM deployments on ``target_id``, in a stable order."""

    routed: list[RoutedModel] = []
    for name, record in model_names(records, target_id).items():
        if record.state != "running":
            continue
        try:
            port = service_port(record.service_id, record.configuration, record.variant_id)
        except ValueError:
            continue
        keyed = supports_api_key(record.service_id) and not is_shareable(record.configuration)
        served = instance_served_model(record)
        routed.append(RoutedModel(name, record.service_id, served, port, keyed))
    return tuple(routed)


def router_inputs(
    store: InfrastructureStore, target_id: str, load_key: Callable[[str, str], str]
) -> RouterInputs:
    """The router's models on ``target_id`` and each keyed instance's key."""

    models = routed_models(store.services(), target_id)
    keys = tuple(
        (model.service_id, load_key(target_id, model.service_id)) for model in models if model.keyed
    )
    return RouterInputs(models=models, upstream_keys=keys)


def _key_variable(index: int) -> str:
    return f"{UPSTREAM_KEY_PREFIX}{index}"


def router_config(models: Iterable[RoutedModel]) -> dict[str, object]:
    """The LiteLLM Proxy configuration for ``models``: no secret, only environment names."""

    entries = []
    for index, model in enumerate(models):
        entries.append(
            {
                "model_name": model.name,
                "litellm_params": {
                    "model": f"hosted_vllm/{model.served}",
                    "api_base": f"http://127.0.0.1:{model.port}/v1",
                    "api_key": f"os.environ/{_key_variable(index)}"
                    if model.keyed
                    else _NO_UPSTREAM_KEY,
                },
            }
        )
    return {
        "model_list": entries,
        "litellm_settings": {
            "telemetry": False,
            "drop_params": False,
            "turn_off_message_logging": True,
            "request_timeout": 600,
            "num_retries": 0,
        },
        "router_settings": {"num_retries": 0, "disable_cooldowns": True},
        "general_settings": {
            "master_key": f"os.environ/{MASTER_KEY_VARIABLE}",
            "store_model_in_db": False,
            "disable_spend_logs": True,
        },
    }


def render_config(models: Iterable[RoutedModel]) -> str:
    """The configuration file's text (JSON, which LiteLLM's YAML loader reads)."""

    return json.dumps(router_config(models), indent=2, sort_keys=True) + "\n"


def config_digest(models: Iterable[RoutedModel]) -> str:
    """Identifies the configuration a router runs with (no secret enters it)."""

    return hashlib.sha256(render_config(models).encode()).hexdigest()


def router_port(configuration: dict[str, str]) -> int:
    """The router's loopback port (chosen free on the target at install)."""

    raw = configuration.get(PORT_FIELD, "").strip()
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ValueError("The model router has no valid port; reinstall it")
    return int(raw)


def router_plan(
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    api_key: str | None,
    inputs: RouterInputs | None,
) -> DriverPlan:
    """Compile one lifecycle action of the model router.

    Raises:
        ValueError: An unsupported host, a start without its key, or a start
            with no running vLLM instance to route to (``router_without_models``).
    """

    if facts.os not in {"linux", "macos"}:
        raise ValueError("The model router runs on a Linux or macOS host")
    if action in {"install", "reinstall"} and not facts.uv_available:
        raise ValueError("The model router is installed with uv; install uv on this host")
    port = router_port(configuration)
    directory = configuration.get("storage.service_directory") or service_directory(
        CONTAINER_NAME, facts, target
    )
    ownership = hashlib.sha256(
        f"{facts.target_id}:{facts.hostname}:{directory}".encode()
    ).hexdigest()
    manifest = {
        "definition_version": DEFINITION_VERSION,
        "project": '[project]\nname="clio-model-router"\nversion="0.0.0"\n'
        'requires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps([f"litellm[proxy]=={LITELLM_VERSION}"])
        + "\n",
        "launcher": LAUNCHER,
        "port": port,
        "health_path": HEALTH_PATH,
        "identity": {"kind": "openai", "served_models_file": MODELS_FILE},
        "arguments": [
            "--config",
            CONFIG_FILE,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--telemetry",
            "False",
        ],
        "environment": dict(PROXY_ENVIRONMENT),
        "installation_bytes": 3 * 1024**3,
    }
    resolved = {
        **configuration,
        "storage.service_directory": directory,
        "native_owner": ownership,
    }
    secret_env: dict[str, str] = {}
    runtime_files: dict[str, str] = {}
    if action == "start":
        if not api_key:
            raise ValueError("The model router always runs with its key")
        models = inputs.models if inputs is not None else ()
        if not models:
            raise ValueError(
                "router_without_models: no vLLM instance is running on this host; "
                "start one before the model router"
            )
        keys = dict(inputs.upstream_keys) if inputs is not None else {}
        for index, model in enumerate(models):
            if model.keyed:
                key = keys.get(model.service_id, "")
                if not key:
                    raise ValueError(
                        f"CLIO has no key for {model.service_id}; reinstall it before routing to it"
                    )
                secret_env[_key_variable(index)] = key
        runtime_files = {
            CONFIG_FILE: render_config(models),
            MODELS_FILE: json.dumps([model.name for model in models]),
        }
        resolved.update(
            {
                MODELS_FIELD: json.dumps(
                    {model.name: model.service_id for model in models}, sort_keys=True
                ),
                DIGEST_FIELD: config_digest(models),
                IDLE_FIELD: "",
            }
        )
    return supervised_plan(
        action,
        directory=directory,
        ownership=ownership,
        manifest=manifest,
        port=port,
        label="Model router",
        configuration=resolved,
        api_key=api_key,
        key_variable=MASTER_KEY_VARIABLE,
        secret_env=secret_env,
        runtime_files=runtime_files,
    )


def router_target(credential_ref: str) -> str | None:
    """The target of a saved server linked to a model router's key, else ``None``."""

    prefix, _, rest = credential_ref.partition(":")
    target_id, _, service_id = rest.rpartition(":")
    if prefix != "managed-server" or service_id != ROUTER_SERVICE or not target_id:
        return None
    return target_id


def router_model_problem(
    store: InfrastructureStore, credential_ref: str, model_id: str
) -> dict[str, object] | None:
    """Why the router a saved server names cannot serve ``model_id`` now, or ``None``.

    Returns a dict with ``error`` -- ``model_router_stopped`` (the router does
    not run), ``model_instance_stopped`` (the instance serving the model does
    not run) or ``model_not_routed`` (no instance on the router's host serves
    it) -- and the facts its message names.
    """

    target_id = router_target(credential_ref)
    if target_id is None:
        return None
    records = store.services()
    router = next(
        (row for row in records if row.target_id == target_id and row.service_id == ROUTER_SERVICE),
        None,
    )
    if router is None:
        return None
    names = model_names(records, target_id)
    # A model ref may carry the wire prefix of the preset it is reached through.
    for prefix in ("hosted_vllm/", "openai/"):
        if model_id not in names and model_id.startswith(prefix):
            model_id = model_id[len(prefix) :]
    record = names.get(model_id)
    if record is not None and record.state != "running":
        return {
            "error": "model_instance_stopped",
            "target_id": target_id,
            "instance": record.service_id,
            "state": record.state,
            "model": model_id,
        }
    if router.state != "running":
        return {"error": "model_router_stopped", "target_id": target_id, "model": model_id}
    if record is None:
        try:
            routed = sorted(json.loads(router.configuration.get(MODELS_FIELD) or "{}"))
        except ValueError:
            routed = []
        return {
            "error": "model_not_routed",
            "target_id": target_id,
            "model": model_id,
            "routed": routed,
        }
    return None


def refreshes_router(service_id: str, action: str) -> bool:
    """Whether a finished ``action`` on ``service_id`` can change what the router serves."""

    return (
        engine_of(service_id) == "vllm"
        and service_id != ROUTER_SERVICE
        and action in {"install", "reinstall", "start", "stop", "uninstall", "delete_data"}
    )


__all__ = [
    "CONFIG_FILE",
    "DIGEST_FIELD",
    "IDLE_FIELD",
    "LITELLM_VERSION",
    "MASTER_KEY_VARIABLE",
    "MODELS_FIELD",
    "MODELS_FILE",
    "ROUTER_VARIANT",
    "RoutedModel",
    "RouterInputs",
    "config_digest",
    "instance_served_model",
    "model_names",
    "refreshes_router",
    "render_config",
    "routed_models",
    "router_config",
    "router_definition",
    "router_inputs",
    "router_model_problem",
    "router_plan",
    "router_port",
    "router_target",
]
