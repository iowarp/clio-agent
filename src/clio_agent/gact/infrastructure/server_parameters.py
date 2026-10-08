"""Typed, per-engine server parameters: declared once, rendered by the UI, compiled to launch.

Each managed model engine declares the server-side knobs a person may tweak
(parallelism, context, threads, memory) as :class:`ServerParameter` rows. The
UI renders the declaration verbatim (it never knows engine flags), the person's
values arrive in the service configuration under ``param.<id>``, and
:func:`compile_parameters` validates them against the declaration and turns
them into the engine's own flags and environment. A value left empty is not
passed at all, so the engine's own default stays in force -- and the effective
value the running server reports is shown back (see
:mod:`clio_agent.gact.infrastructure.effective_parameters`).

Only declared parameters reach the launch: an unknown ``param.*`` key, a value
of the wrong type, or one outside its bounds is a ``ValueError`` naming the
field, never silently dropped or clamped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

ParameterKind = Literal["integer", "number", "choice", "text"]
ParameterDelivery = Literal["flag", "env"]
EngineId = Literal["vllm", "llama_cpp", "ollama"]

#: Configuration keys carrying a server parameter are ``param.<id>``.
PARAMETER_PREFIX = "param."

#: A choice that turns a model-derived default off: validated and recorded, no flag.
OFF = "off"


class ServerParameter(BaseModel):
    """One tweakable server parameter as declared by an engine driver."""

    id: str
    label: str
    description: str
    kind: ParameterKind
    #: The engine flag (``--max-num-seqs``) or environment variable
    #: (``OLLAMA_NUM_PARALLEL``) the value is delivered as.
    delivery: ParameterDelivery
    name: str
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = Field(default_factory=list)
    #: Variant ids the parameter applies to; empty means every variant.
    variants: list[str] = Field(default_factory=list)
    #: What the engine does when the value is left empty.
    default_behavior: str = ""
    #: The effective-value key the running server reports this parameter as.
    effective_key: str = ""
    #: Flags the engine needs alongside this one (vLLM's tool-call parser only
    #: takes effect with ``--enable-auto-tool-choice``).
    companion_flags: list[str] = Field(default_factory=list)

    def applies_to(self, variant_id: str) -> bool:
        """Whether this parameter is meaningful for ``variant_id``."""

        return not self.variants or variant_id in self.variants


def _int(
    pid: str,
    label: str,
    description: str,
    delivery: ParameterDelivery,
    name: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
    variants: list[str] | None = None,
    default_behavior: str = "",
    effective_key: str = "",
) -> ServerParameter:
    return ServerParameter(
        id=pid,
        label=label,
        description=description,
        kind="integer",
        delivery=delivery,
        name=name,
        minimum=minimum,
        maximum=maximum,
        variants=variants or [],
        default_behavior=default_behavior,
        effective_key=effective_key or pid,
    )


_GPU = ["cuda", "rocm"]

ENGINE_PARAMETERS: dict[EngineId, tuple[ServerParameter, ...]] = {
    "vllm": (
        _int(
            "tensor_parallel_size",
            "Tensor parallel size",
            "Split each layer across this many devices (GPUs, or CPU NUMA nodes on the CPU build).",
            "flag",
            "--tensor-parallel-size",
            maximum=64,
            default_behavior="1",
        ),
        _int(
            "pipeline_parallel_size",
            "Pipeline parallel size",
            "Split the layer stack into this many sequential stages.",
            "flag",
            "--pipeline-parallel-size",
            maximum=64,
            default_behavior="1",
        ),
        _int(
            "max_num_seqs",
            "Concurrent sequences",
            "Most requests the server batches at once.",
            "flag",
            "--max-num-seqs",
            maximum=4096,
            default_behavior="vLLM's default for this hardware",
        ),
        _int(
            "max_model_len",
            "Context length",
            "Longest prompt plus completion the server accepts, in tokens.",
            "flag",
            "--max-model-len",
            minimum=16,
            maximum=10_000_000,
            default_behavior="The model's own maximum",
        ),
        ServerParameter(
            id="gpu_memory_utilization",
            label="GPU memory fraction",
            description="Share of each GPU's memory vLLM may reserve (0.05 to 1).",
            kind="number",
            delivery="flag",
            name="--gpu-memory-utilization",
            minimum=0.05,
            maximum=1.0,
            variants=_GPU,
            default_behavior="0.9",
            effective_key="gpu_memory_utilization",
        ),
        _int(
            "cpu_kvcache_space",
            "KV cache memory (GiB)",
            "Host memory reserved for the key-value cache on the CPU build.",
            "env",
            "VLLM_CPU_KVCACHE_SPACE",
            maximum=4096,
            variants=["cpu"],
            default_behavior="vLLM's CPU default",
        ),
        ServerParameter(
            id="cpu_omp_threads_bind",
            label="CPU cores",
            description="Cores the CPU build pins its threads to, such as 0-15, or auto.",
            kind="text",
            delivery="env",
            name="VLLM_CPU_OMP_THREADS_BIND",
            variants=["cpu"],
            default_behavior="auto",
            effective_key="cpu_omp_threads_bind",
        ),
        ServerParameter(
            id="tool_call_parser",
            label="Tool-call parser",
            description=(
                "Turns on tool calling with the parser for this model family (Qwen2.5 and "
                "Hermes-style templates use hermes). Without it vLLM refuses tool calls."
            ),
            kind="choice",
            delivery="flag",
            name="--tool-call-parser",
            options=[
                OFF,
                "hermes",
                "mistral",
                "llama3_json",
                "llama4_pythonic",
                "pythonic",
                "granite",
                "internlm",
                "xlam",
                "qwen3_coder",
                "qwen3_xml",
                "deepseek_v3",
                "kimi_k2",
                "glm45",
                "openai",
            ],
            default_behavior="Chosen from the model family (off when the family is unknown)",
            effective_key="tool_call_parser",
            companion_flags=["--enable-auto-tool-choice"],
        ),
        ServerParameter(
            id="reasoning_parser",
            label="Reasoning parser",
            description=(
                "Separates the model's thinking from its answer and returns it as "
                "reasoning_content (Qwen3 uses qwen3). Without it a thinking model's "
                "<think> text arrives inside the answer."
            ),
            kind="choice",
            delivery="flag",
            name="--reasoning-parser",
            options=[
                OFF,
                "qwen3",
                "deepseek_r1",
                "deepseek_v3",
                "openai_gptoss",
                "granite",
                "glm45",
                "hunyuan_a13b",
                "kimi_k2",
                "minimax_m2",
                "mistral",
                "seed_oss",
                "step3",
            ],
            default_behavior=(
                "Chosen from the model family (thinking left inside the answer when unknown)"
            ),
            effective_key="reasoning_parser",
        ),
        ServerParameter(
            id="dtype",
            label="Weight precision",
            description="Data type for weights and activations.",
            kind="choice",
            delivery="flag",
            name="--dtype",
            options=["auto", "bfloat16", "float16", "float32"],
            default_behavior="auto",
            effective_key="dtype",
        ),
    ),
    "llama_cpp": (
        _int(
            "parallel",
            "Parallel slots",
            "Requests served at once; the context is shared across slots.",
            "flag",
            "--parallel",
            maximum=256,
            default_behavior="llama.cpp's automatic slot count",
            effective_key="parallel",
        ),
        _int(
            "ctx_size",
            "Context size",
            "Total context in tokens across all slots (0 uses the model's own).",
            "flag",
            "--ctx-size",
            minimum=0,
            maximum=10_000_000,
            default_behavior="The model's trained context",
            effective_key="ctx_size",
        ),
        _int(
            "threads",
            "Threads",
            "CPU threads used for generation.",
            "flag",
            "--threads",
            maximum=1024,
            default_behavior="llama.cpp picks from the available cores",
        ),
        _int(
            "gpu_layers",
            "GPU layers",
            "Model layers offloaded to the GPU.",
            "flag",
            "--n-gpu-layers",
            minimum=0,
            maximum=10_000,
            default_behavior="Every layer on the GPU",
            variants=["cuda", "vulkan"],
        ),
    ),
    "ollama": (
        _int(
            "num_parallel",
            "Parallel requests",
            "Requests each loaded model serves at once.",
            "env",
            "OLLAMA_NUM_PARALLEL",
            maximum=256,
            default_behavior="Ollama's automatic choice",
            effective_key="num_parallel",
        ),
        _int(
            "context_length",
            "Context length",
            "Default context window for loaded models, in tokens.",
            "env",
            "OLLAMA_CONTEXT_LENGTH",
            minimum=256,
            maximum=10_000_000,
            default_behavior="Ollama's default",
            effective_key="context_length",
        ),
    ),
}


def engine_parameters(engine: EngineId, variant_id: str | None = None) -> list[ServerParameter]:
    """The declared parameters for ``engine`` (only those for ``variant_id`` when given)."""

    rows = ENGINE_PARAMETERS[engine]
    return [row for row in rows if variant_id is None or row.applies_to(variant_id)]


@dataclass(frozen=True)
class CompiledParameters:
    """Validated parameter values as engine flags and environment.

    Attributes:
        flags: Flag/value pairs appended to the engine command line.
        env: Environment variable/value pairs for the server.
        values: The validated values by parameter id (what was requested).
    """

    flags: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    values: dict[str, str]


def _validated(parameter: ServerParameter, raw: str) -> str:
    value = raw.strip()
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError(f"{parameter.label} contains control characters")
    if parameter.kind == "integer":
        # ASCII digits only: int() also takes "1_000" and non-ASCII digits,
        # which an engine's own parser reads differently.
        if not value.lstrip("-").isascii() or not value.lstrip("-").isdigit():
            raise ValueError(f"{parameter.label} must be a whole number")
        number: float = int(value)
        value = str(int(value))
    elif parameter.kind == "number":
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError(f"{parameter.label} must be a number") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"{parameter.label} must be a finite number")
        value = repr(number)
    elif parameter.kind == "choice":
        if value not in parameter.options:
            raise ValueError(f"{parameter.label} must be one of {', '.join(parameter.options)}")
        return value
    else:
        if value.startswith("-") or len(value) > 256:
            raise ValueError(f"{parameter.label} is not a valid value")
        return value
    if parameter.minimum is not None and number < parameter.minimum:
        raise ValueError(f"{parameter.label} must be at least {parameter.minimum:g}")
    if parameter.maximum is not None and number > parameter.maximum:
        raise ValueError(f"{parameter.label} must be at most {parameter.maximum:g}")
    return value


def compile_parameters(
    engine: EngineId, variant_id: str, configuration: dict[str, str]
) -> CompiledParameters:
    """Validate ``param.*`` configuration values and compile them for launch.

    Args:
        engine: The engine whose declaration applies.
        variant_id: The variant being launched (some parameters are variant-only).
        configuration: The service configuration; ``param.<id>`` keys are read.

    Returns:
        The flags, environment and validated values to launch with.

    Raises:
        ValueError: For an undeclared parameter, one that does not apply to
            the variant, or a value of the wrong type or out of bounds.
    """

    declared = {row.id: row for row in ENGINE_PARAMETERS[engine]}
    flags: list[str] = []
    env: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    for key, raw in configuration.items():
        if not key.startswith(PARAMETER_PREFIX):
            continue
        pid = key[len(PARAMETER_PREFIX) :]
        parameter = declared.get(pid)
        if parameter is None:
            raise ValueError(f"{pid!r} is not a server parameter of this engine")
        if not str(raw).strip():
            continue
        if not parameter.applies_to(variant_id):
            raise ValueError(f"{parameter.label} does not apply to the {variant_id} variant")
        value = _validated(parameter, str(raw))
        values[pid] = value
    for parameter in ENGINE_PARAMETERS[engine]:
        chosen = values.get(parameter.id)
        if chosen is None or (chosen == OFF and OFF in parameter.options):
            continue
        if parameter.delivery == "flag":
            flags.extend([*parameter.companion_flags, parameter.name, chosen])
        else:
            env.append((parameter.name, chosen))
    return CompiledParameters(flags=tuple(flags), env=tuple(env), values=values)
