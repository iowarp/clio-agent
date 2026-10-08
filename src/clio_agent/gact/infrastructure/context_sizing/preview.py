"""``POST /v1/infrastructure/services/{service}/context-sizing``: the control before deploying.

A deployment form shows a number input, a Max button and a Fit-to-GPU
selector for the engine's context. Their values depend on the model the form
names and the host's GPUs, so this preview reads them the same way a launch
would (:mod:`.deployment`): the model's layout and GPU memory through the
host-side probe for vLLM and llama.cpp, the running server for Ollama (its
model's ``model_info`` exists only once pulled). Nothing is changed on the host.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from clio_agent.context_sizing.controls import ContextControls
from clio_agent.gact.infrastructure.context_sizing.deployment import (
    deployment_controls,
    gather_inputs,
    sizing_request,
)
from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.infrastructure.server_parameters import EngineId


class ContextSizingPreviewRequest(BaseModel):
    """The deployment form's state: where, which variant, and its configuration."""

    model_config = ConfigDict(extra="forbid")

    target_id: str = "local"
    #: Empty: the installed deployment's variant.
    variant_id: str = ""
    configuration: dict[str, str] = Field(default_factory=dict)


async def preview_context(
    runtime: Any, service_id: str, request: ContextSizingPreviewRequest
) -> ContextControls:
    """The context control for ``request`` on its host.

    Args:
        runtime: The :class:`~clio_agent.gact.infrastructure.runtime.InfrastructureRuntime`.
        service_id: ``vllm``, ``llama_cpp`` or ``ollama``.
        request: The form's state; typed values override the installed ones.

    Raises:
        KeyError: Unknown target or service.
        ValueError: An invalid context choice, strategy or GPU share.
    """

    from clio_agent.gact.infrastructure.container_runtime import (  # noqa: PLC0415
        logs_command,
        parse_runtime_name,
    )
    from clio_agent.gact.infrastructure.model_instances import (  # noqa: PLC0415
        engine_of,
        validate_service_id,
    )
    from clio_agent.gact.infrastructure.model_runtimes import (  # noqa: PLC0415
        ENGINES,
        MODEL_RUNTIME_SERVICES,
        service_port,
    )
    from clio_agent.gact.infrastructure.ollama_context_apply import (  # noqa: PLC0415
        read_ollama_inputs,
    )
    from clio_agent.gact.infrastructure.probe import probe_target  # noqa: PLC0415

    if engine_of(service_id) not in MODEL_RUNTIME_SERVICES:
        raise KeyError(service_id)
    validate_service_id(service_id)
    target = runtime.store.target(request.target_id)
    if target is None:
        raise KeyError(request.target_id)
    engine = cast(EngineId, engine_of(service_id))
    installed = runtime.store.service(request.target_id, service_id)
    typed = {key: value for key, value in request.configuration.items() if value.strip()}
    configuration = {**(installed.configuration if installed else {}), **typed}
    variant_id = request.variant_id or (installed.variant_id if installed else "")
    sizing = sizing_request(engine, configuration)

    async def execute(spec: CommandSpec) -> CommandResult:
        return cast(CommandResult, await runtime.execute_on_target(request.target_id, spec))

    facts = await probe_target(target, execute if target.kind == "ssh" else None)
    if engine != "ollama":
        profile, budget, why = await gather_inputs(
            engine, variant_id, configuration, facts, sizing, execute
        )
    elif installed is not None and installed.state == "running" and configuration.get("model"):
        logs = logs_command(
            parse_runtime_name(configuration.get("container_runtime") or "docker"),
            ENGINES["ollama"].container_name,
            lines=40,
        )
        profile, budget, why = await read_ollama_inputs(
            execute,
            service_port("ollama", configuration),
            configuration["model"],
            logs,
            facts.os == "windows",
            sizing,
        )
    else:
        profile, budget, why = None, None, "Ollama reads the model once it is deployed and pulled"
    return deployment_controls(
        engine,
        sizing,
        profile,
        budget,
        why=why,
        installed=installed.configuration if installed is not None else None,
    )


__all__ = ["ContextSizingPreviewRequest", "preview_context"]
