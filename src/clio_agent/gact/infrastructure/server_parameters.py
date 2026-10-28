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

Device pins (``value_format == "device_list"``: ``cuda_devices``,
``rocm_devices``) are comma lists of distinct non-negative GPU ids, normalised
to ``1,2``; a malformed one is a :class:`GpuDeviceError` (``gpu_device_invalid``).
Whether the ids exist on the host and match the engine's parallelism is checked
against the host's facts in :mod:`~clio_agent.gact.infrastructure.gpu_devices`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from clio_agent.context_sizing.controls import ContextSizingSpec

ParameterKind = Literal["integer", "number", "choice", "text"]
ParameterDelivery = Literal["flag", "env"]
#: How a ``text`` value is read: free text, GPU ids (``0,2``) or numbers (``3,1``).
ParameterFormat = Literal["", "device_list", "number_list"]
GpuDeviceReason = Literal["gpu_device_invalid", "gpu_device_unknown", "gpu_device_count_mismatch"]
EngineId = Literal["vllm", "llama_cpp", "ollama"]

#: Configuration keys carrying a server parameter are ``param.<id>``.
PARAMETER_PREFIX = "param."

#: A choice that turns a model-derived default off: validated and recorded, no flag.
OFF = "off"


class GpuDeviceError(ValueError):
    """A deployment's GPU pin CLIO cannot launch (``reason`` is the typed code).

    Attributes:
        reason: ``gpu_device_invalid`` (not distinct non-negative ids),
            ``gpu_device_unknown`` (an id the host does not list) or
            ``gpu_device_count_mismatch`` (vLLM's tensor x pipeline parallel
            size differs from the number of pinned devices).
        parameter: The parameter id (``cuda_devices``).
        details: Typed details for the API envelope.
    """

    def __init__(
        self,
        reason: GpuDeviceReason,
        message: str,
        *,
        parameter: str = "",
        **details: object,
    ) -> None:
        self.reason: GpuDeviceReason = reason
        self.parameter = parameter
        self._details = {"parameter": parameter, **details}
        super().__init__(f"{reason}: {message}")

    def details(self) -> dict[str, object]:
        """The typed error's details for an API envelope."""

        return dict(self._details)


def parse_device_ids(
    raw: str, *, label: str = "GPU devices", parameter: str = ""
) -> tuple[int, ...]:
    """The GPU ids of a device pin (``"1, 2"`` -> ``(1, 2)``), in the person's order.

    Raises:
        GpuDeviceError: ``gpu_device_invalid`` for an empty entry, a value that
            is not a non-negative whole number, or an id named twice.
    """

    ids: list[int] = []
    for part in str(raw).split(","):
        entry = part.strip()
        if not entry.isascii() or not entry.isdigit():
            raise GpuDeviceError(
                "gpu_device_invalid",
                f"{label} must be GPU numbers such as 0 or 1,2 (got {raw.strip()!r})",
                parameter=parameter,
                value=raw.strip(),
            )
        device = int(entry)
        if device in ids:
            raise GpuDeviceError(
                "gpu_device_invalid",
                f"{label} names GPU {device} twice",
                parameter=parameter,
                value=raw.strip(),
            )
        ids.append(device)
    return tuple(ids)


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
    #: Environment the engine needs alongside this one (a CUDA pin also sets
    #: ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` so its ids are ``nvidia-smi``'s).
    companion_env: dict[str, str] = Field(default_factory=dict)
    #: How a ``text`` value is read; ``device_list`` values are GPU ids the
    #: UI offers from ``facts.gpus``.
    value_format: ParameterFormat = ""
    #: The context parameter's Max / Fit-to-GPU control (see ``context_sizing``);
    #: set per host on the catalog, absent on every other parameter.
    context_sizing: ContextSizingSpec | None = None

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


_GPU = ["cuda", "rocm", "xpu"]
#: llama.cpp backends that place layers on GPUs (container and native ids).
_LLAMA_GPU = ["cuda", "vulkan", "rocm", "metal"]
#: Makes CUDA number devices as ``nvidia-smi`` (and ``facts.gpus``) does.
_PCI_ORDER = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}


def _devices(
    pid: str,
    label: str,
    description: str,
    name: str,
    variants: list[str],
    *,
    companion_env: dict[str, str] | None = None,
) -> ServerParameter:
    """A GPU pin: a comma list of device ids delivered as a visible-devices variable."""

    return ServerParameter(
        id=pid,
        label=label,
        description=description,
        kind="text",
        delivery="env",
        name=name,
        variants=variants,
        default_behavior="All devices this host exposes",
        effective_key=pid,
        companion_env=companion_env or {},
        value_format="device_list",
    )


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
            default_behavior="Fit to GPU: the model's maximum, capped to fit the GPU",
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
            id="xpu_devices",
            label="Intel GPU devices",
            description=(
                "Level Zero devices the server may use, such as 0,1 (in FLAT mode each GPU "
                "tile is one device). Match the count to the tensor parallel size."
            ),
            kind="text",
            delivery="env",
            name="ZE_AFFINITY_MASK",
            variants=["xpu"],
            default_behavior="All devices this host exposes",
            effective_key="xpu_devices",
        ),
        _devices(
            "cuda_devices",
            "NVIDIA GPU devices",
            "CUDA devices the server may use, such as 1 or 2,3 (nvidia-smi numbering), so "
            "instances on one host do not share a GPU. The count must equal tensor parallel "
            "size x pipeline parallel size.",
            "CUDA_VISIBLE_DEVICES",
            ["cuda"],
            companion_env=_PCI_ORDER,
        ),
        _devices(
            "rocm_devices",
            "AMD GPU devices",
            "ROCm devices the server may use, such as 1 or 2,3 (rocm-smi numbering). The "
            "count must equal tensor parallel size x pipeline parallel size.",
            "HIP_VISIBLE_DEVICES",
            ["rocm"],
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
                "nemotron_v3",
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
            default_behavior="Fit to GPU: the model's trained context, capped to fit the GPU",
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
            variants=_LLAMA_GPU,
        ),
        _devices(
            "cuda_devices",
            "NVIDIA GPU devices",
            "CUDA devices the server may use, such as 1 or 2,3 (nvidia-smi numbering); "
            "llama.cpp splits the model over every device listed.",
            "CUDA_VISIBLE_DEVICES",
            ["cuda"],
            companion_env=_PCI_ORDER,
        ),
        _devices(
            "rocm_devices",
            "AMD GPU devices",
            "ROCm devices the server may use, such as 1 or 2,3 (rocm-smi numbering); "
            "llama.cpp splits the model over every device listed.",
            "HIP_VISIBLE_DEVICES",
            ["rocm"],
        ),
        ServerParameter(
            id="split_mode",
            label="Multi-GPU split",
            description=(
                "How the model is spread over several GPUs: none (one GPU), layer (whole "
                "layers per GPU) or row (each layer's rows split across GPUs)."
            ),
            kind="choice",
            delivery="flag",
            name="--split-mode",
            options=["none", "layer", "row"],
            variants=_LLAMA_GPU,
            default_behavior="layer",
            effective_key="split_mode",
        ),
        ServerParameter(
            id="tensor_split",
            label="GPU split proportions",
            description=(
                "Share of the model each visible GPU holds, in device order, such as 3,1 "
                "for three quarters on the first GPU."
            ),
            kind="text",
            delivery="flag",
            name="--tensor-split",
            variants=_LLAMA_GPU,
            default_behavior="Proportional to each GPU's free memory",
            effective_key="tensor_split",
            value_format="number_list",
        ),
        _int(
            "main_gpu",
            "Main GPU",
            "The GPU (counted among the visible devices, from 0) that holds the whole model "
            "with split mode none, or the intermediate results with row.",
            "flag",
            "--main-gpu",
            minimum=0,
            maximum=1024,
            variants=_LLAMA_GPU,
            default_behavior="0",
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
            default_behavior="Fit to GPU: the model's trained context, capped to fit the GPU",
            effective_key="context_length",
        ),
        _devices(
            "cuda_devices",
            "NVIDIA GPU devices",
            "CUDA devices Ollama may use, such as 1 or 2,3 (nvidia-smi numbering).",
            "CUDA_VISIBLE_DEVICES",
            ["cuda"],
            companion_env=_PCI_ORDER,
        ),
        _devices(
            "rocm_devices",
            "AMD GPU devices",
            "ROCm devices Ollama may use, such as 1 or 2,3 (rocm-smi numbering).",
            "ROCR_VISIBLE_DEVICES",
            ["rocm"],
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
    elif parameter.value_format == "device_list":
        ids = parse_device_ids(value, label=parameter.label, parameter=parameter.id)
        return ",".join(str(device) for device in ids)
    elif parameter.value_format == "number_list":
        parts = [part.strip() for part in value.split(",")]
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"{parameter.label} must be numbers such as 3,1") from exc
        if any(n != n or n < 0 or n == float("inf") for n in numbers) or not any(numbers):
            raise ValueError(f"{parameter.label} must be non-negative numbers, not all zero")
        return ",".join(parts)
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
        env.extend(
            (name, value)
            for name, value in parameter.companion_env.items()
            if all(name != present for present, _ in env)
        )
    return CompiledParameters(flags=tuple(flags), env=tuple(env), values=values)
