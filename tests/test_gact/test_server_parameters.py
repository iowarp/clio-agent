"""Typed per-engine server parameters compile to each engine's own launch flags and env."""

from __future__ import annotations

import pytest

from clio_agent.gact.infrastructure.server_parameters import (
    compile_parameters,
    engine_parameters,
)


def test_vllm_parallelism_and_context_become_vllm_flags() -> None:
    compiled = compile_parameters(
        "vllm",
        "cuda",
        {
            "param.tensor_parallel_size": "2",
            "param.pipeline_parallel_size": "2",
            "param.max_num_seqs": "64",
            "param.max_model_len": "8192",
            "param.gpu_memory_utilization": "0.85",
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
        },
    )

    assert compiled.flags == (
        "--tensor-parallel-size",
        "2",
        "--pipeline-parallel-size",
        "2",
        "--max-num-seqs",
        "64",
        "--max-model-len",
        "8192",
        "--gpu-memory-utilization",
        "0.85",
    )
    assert compiled.env == ()


def test_vllm_cpu_memory_and_cores_are_environment_on_the_cpu_build() -> None:
    compiled = compile_parameters(
        "vllm",
        "cpu",
        {
            "param.cpu_kvcache_space": "4",
            "param.cpu_omp_threads_bind": "0-7",
            "param.dtype": "float32",
        },
    )

    assert compiled.flags == ("--dtype", "float32")
    assert compiled.env == (("VLLM_CPU_KVCACHE_SPACE", "4"), ("VLLM_CPU_OMP_THREADS_BIND", "0-7"))


def test_a_gpu_only_parameter_is_refused_on_the_cpu_variant() -> None:
    with pytest.raises(ValueError, match="does not apply to the cpu variant"):
        compile_parameters("vllm", "cpu", {"param.gpu_memory_utilization": "0.5"})


def test_llama_cpp_slots_context_and_threads_become_llama_server_flags() -> None:
    compiled = compile_parameters(
        "llama_cpp", "cpu", {"param.parallel": "4", "param.ctx_size": "8192", "param.threads": "8"}
    )

    assert compiled.flags == ("--parallel", "4", "--ctx-size", "8192", "--threads", "8")


def test_ollama_parallelism_and_context_are_ollama_environment() -> None:
    compiled = compile_parameters(
        "ollama", "cpu", {"param.num_parallel": "2", "param.context_length": "4096"}
    )

    assert compiled.flags == ()
    assert compiled.env == (("OLLAMA_NUM_PARALLEL", "2"), ("OLLAMA_CONTEXT_LENGTH", "4096"))


def test_an_empty_value_keeps_the_engine_default_by_passing_nothing() -> None:
    compiled = compile_parameters("llama_cpp", "cpu", {"param.parallel": "", "param.threads": "  "})

    assert compiled.flags == ()
    assert compiled.values == {}


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"param.parallel": "two"}, "whole number"),
        ({"param.parallel": "0"}, "at least 1"),
        ({"param.ctx_size": "-5"}, "at least 0"),
        ({"param.bogus": "1"}, "not a server parameter"),
        ({"param.threads": "4\n--api-key x"}, "control characters"),
    ],
)
def test_invalid_values_are_refused_with_the_field_named(
    configuration: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        compile_parameters("llama_cpp", "cpu", configuration)


def test_choice_and_text_values_are_validated() -> None:
    with pytest.raises(ValueError, match="must be one of"):
        compile_parameters("vllm", "cpu", {"param.dtype": "int4"})
    with pytest.raises(ValueError, match="not a valid value"):
        compile_parameters("vllm", "cpu", {"param.cpu_omp_threads_bind": "--evil"})
    with pytest.raises(ValueError, match="at most 1"):
        compile_parameters("vllm", "cuda", {"param.gpu_memory_utilization": "1.5"})


def test_declarations_are_filtered_by_variant() -> None:
    cpu = {row.id for row in engine_parameters("vllm", "cpu")}
    cuda = {row.id for row in engine_parameters("vllm", "cuda")}

    assert {"cpu_kvcache_space", "cpu_omp_threads_bind"} <= cpu
    assert "gpu_memory_utilization" not in cpu
    assert "gpu_memory_utilization" in cuda
    assert "cpu_kvcache_space" not in cuda
    assert {
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "max_num_seqs",
        "max_model_len",
    } <= cpu & cuda


def test_numbers_are_normalized_and_non_finite_values_refused() -> None:
    with pytest.raises(ValueError, match="whole number"):
        compile_parameters("llama_cpp", "cpu", {"param.parallel": "1_000"})
    with pytest.raises(ValueError, match="whole number"):
        compile_parameters("llama_cpp", "cpu", {"param.parallel": "\u0663"})
    with pytest.raises(ValueError, match="finite"):
        compile_parameters("vllm", "cuda", {"param.gpu_memory_utilization": "nan"})
    compiled = compile_parameters("llama_cpp", "cpu", {"param.parallel": "007"})
    assert compiled.flags == ("--parallel", "7")
