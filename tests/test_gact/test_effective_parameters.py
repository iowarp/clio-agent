"""Effective server parameters, read from real server output captured on ares."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.gact.infrastructure.effective_parameters import (
    assemble,
    parse_container_config,
    parse_llama_props,
    parse_ollama_ps,
    parse_ollama_server_config,
    parse_vllm_metrics,
    parse_vllm_models,
)
from clio_agent.gact.infrastructure.runtime_probe import parse_runtime_lines

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "managed_servers"


def _wrap(text: str, width: int = 100) -> str:
    """What the Desktop's 100-column ConPTY returns for one long line.

    Verbatim shape seen on ares: each wrap is CRLF, a cursor move to the last
    column, and a redraw of the character already there.
    """

    chunks = [text[i : i + width] for i in range(0, len(text), width)]
    out = chunks[0]
    for chunk in chunks[1:]:
        out += f"\r\n\x1b[29;{width}H{out[-1]}{chunk}"
    return out + "\r\n"


def test_the_verbatim_wrapped_inspect_seen_through_the_desktop_parses() -> None:
    seen = (
        '["-hf","Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M","--host","0.0.0.0","--port",'
        '"8088","--parallel","2",\r\n\x1b[29;100H,"--ctx-size","8192","--threads","4"] '
        '|clio| ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin\r\n\x1b[29;100Hn'
        ':/sbin:/bin","TERM=xterm","HOME=/cache","HOSTNAME=7c2\r\n\x1b[29;100H2b20600b08"]'
    )

    args, env = parse_container_config(seen)

    assert args[-6:] == ["--parallel", "2", "--ctx-size", "8192", "--threads", "4"]
    assert env["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    assert env["HOSTNAME"] == "7c2b20600b08"


def test_llama_cpp_props_report_slots_and_per_slot_context() -> None:
    report = parse_llama_props((FIXTURES / "llama_cpp_b11206_props.json").read_text())

    # Launched with --parallel 2 --ctx-size 8192: two slots of 4096 each.
    assert report["parallel"][0] == "2"
    assert report["context_per_slot"][0] == "4096"


def test_podman_inspect_survives_the_desktop_terminal_wrapping_it() -> None:
    raw = (FIXTURES / "podman_inspect_llama_cpp.txt").read_text().strip()
    assert len(raw) > 100

    args, env = parse_container_config(_wrap(raw))

    assert args[args.index("--ctx-size") + 1] == "8192"
    assert args[args.index("--threads") + 1] == "4"
    assert env["LLAMA_CACHE"] == "/cache/llama.cpp"


def test_llama_cpp_rows_prefer_the_server_then_the_container_launch() -> None:
    raw = (FIXTURES / "podman_inspect_llama_cpp.txt").read_text()
    args, env = parse_container_config(raw)
    rows = {
        row.id: row
        for row in assemble(
            "llama_cpp",
            "cpu",
            report=parse_llama_props((FIXTURES / "llama_cpp_b11206_props.json").read_text()),
            container_args=args,
            container_env=env,
            requested={},
        )
    }

    assert (rows["parallel"].value, rows["parallel"].source) == ("2", "server_report")
    assert (rows["ctx_size"].value, rows["ctx_size"].source) == ("8192", "container_config")
    assert (rows["threads"].value, rows["threads"].source) == ("4", "container_config")
    assert (rows["context_per_slot"].value, rows["context_per_slot"].source) == (
        "4096",
        "server_report",
    )


def test_unset_parameters_are_labelled_engine_default_and_apptainer_uses_the_request() -> None:
    rows = {
        row.id: row
        for row in assemble(
            "llama_cpp",
            "cpu",
            report={},
            container_args=None,
            container_env=None,
            requested={"threads": "6"},
        )
    }

    assert rows["threads"].source == "launch_request" and rows["threads"].value == "6"
    assert rows["parallel"].source == "engine_default"


def test_ollama_server_config_line_is_read_even_when_wrapped() -> None:
    line = (
        'time=2026-09-27T07:22:47.466Z level=INFO source=routes.go:1606 msg="server config" '
        'env="map[CUDA_VISIBLE_DEVICES: GGML_VK_VISIBLE_DEVICES: HTTPS_PROXY: HTTP_PROXY: '
        "NO_PROXY: OLLAMA_CONTEXT_LENGTH:4096 OLLAMA_DEBUG:INFO OLLAMA_FLASH_ATTENTION:false "
        "OLLAMA_HOST:http://0.0.0.0:11434 OLLAMA_KEEP_ALIVE:5m0s OLLAMA_MODELS:/cache/models "
        'OLLAMA_NUM_PARALLEL:2 OLLAMA_ORIGINS:[]]"'
    )

    report = parse_ollama_server_config("starting\n" + _wrap(line) + "Listening on [::]:11434\n")

    assert report["num_parallel"][0] == "2"
    assert report["context_length"][0] == "4096"


def test_ollama_ps_reports_each_loaded_models_context() -> None:
    rows = parse_ollama_ps(
        json.dumps({"models": [{"name": "qwen2.5:0.5b", "context_length": 4096}]})
    )

    assert [(row.label, row.value) for row in rows] == [("Loaded context (qwen2.5:0.5b)", "4096")]
    assert parse_ollama_ps(json.dumps({"models": []})) == []


def test_vllm_models_and_metrics_payloads() -> None:
    models = json.dumps(
        {"object": "list", "data": [{"id": "Qwen/Qwen2.5-0.5B-Instruct", "max_model_len": 4096}]}
    )
    metrics = 'vllm:cache_config_info{block_size="16",gpu_memory_utilization="0.9",num_cpu_blocks="1"} 1.0\n'

    assert parse_vllm_models(models)["max_model_len"][0] == "4096"
    assert parse_vllm_metrics(metrics)["gpu_memory_utilization"][0] == "0.9"
    assert parse_vllm_models("not json") == {}
    assert parse_vllm_metrics("") == {}


def test_a_wrapped_runtime_probe_keeps_the_whole_reason() -> None:
    reason = 'time="2026-09-27T01:13:17-05:00" level=error msg="stat /run/user/1008: no such file or directory"'
    stdout = (
        "Linux|x86_64|none|1|1|0\r\n"
        + _wrap(f"rt|podman|1|0||{reason}")
        + "id|1008|65534|/home/alice\r\n"
    )

    runtimes, identity, home = parse_runtime_lines(stdout)

    assert runtimes[0].detail == reason
    assert identity.uid == 1008 and home == "/home/alice"


def test_a_created_directory_line_that_is_not_the_requested_path_is_never_recorded() -> None:
    from clio_agent.gact.infrastructure.models import CommandResult
    from clio_agent.gact.infrastructure.resource_ledger import directory_recorder

    record = directory_recorder("/home/alice/.local/share/clio/services/ares/clio-vllm/cache")
    # A 100-column terminal cut the line: the truncated prefix names an
    # ancestor as if it were the requested directory.
    rows = record(
        CommandResult(
            exit_code=0,
            stdout="CLIO_CREATED_PARENT /home/alice/.local/share/clio\n"
            "CLIO_CREATED_DIR /home/alice/.local/share\n"
            "CLIO_CREATED_PARENT /etc\n",
        )
    )

    assert [(row.kind, row.ref) for row in rows] == [
        ("parent_directory", "/home/alice/.local/share/clio")
    ]


def test_vllm_reports_the_dtype_auto_resolved_to_and_its_parallel_layout() -> None:
    from clio_agent.gact.infrastructure.effective_parameters import parse_vllm_engine_config

    line = (
        "(EngineCore pid=255) INFO 09-27 10:08:31 [core.py:95] Initializing a V1 LLM engine "
        "(v0.28.0) with config: model='Qwen/Qwen2.5-0.5B-Instruct', speculative_config=None, "
        "tokenizer='Qwen/Qwen2.5-0.5B-Instruct', skip_tokenizer_init=False, tokenizer_mode=auto, "
        "revision=None, trust_remote_code=False, dtype=torch.bfloat16, max_seq_len=4096, "
        "download_dir=None, load_format=auto, tensor_parallel_size=1, pipeline_parallel_size=1, "
        "data_parallel_size=1, disable_custom_all_reduce=True, quantization=None"
    )

    report = parse_vllm_engine_config(_wrap(line))

    assert report["dtype"][0] == "bfloat16"
    assert "dtype=torch.bfloat16" in report["dtype"][1]
    assert report["tensor_parallel_size"][0] == "1"
    assert report["pipeline_parallel_size"][0] == "1"
    assert parse_vllm_engine_config("") == {}
    rows = {
        row.id: row
        for row in assemble(
            "vllm",
            "cpu",
            report=report,
            container_args=["--model", "m"],
            container_env={},
            requested={"dtype": "auto"},
        )
    }
    assert (rows["dtype"].value, rows["dtype"].source) == ("bfloat16", "server_report")


def test_an_unset_ollama_context_length_reports_the_vram_based_default() -> None:
    logs = (
        'time=2026-09-27T07:22:47Z level=INFO source=routes.go:1606 msg="server config" '
        'env="map[OLLAMA_CONTEXT_LENGTH:0 OLLAMA_NUM_PARALLEL:1]"\n'
        'time=2026-09-27T07:22:47Z level=INFO source=routes.go:1700 msg="vram-based default '
        'context" total_vram="0 B" default_num_ctx=4096\n'
    )

    report = parse_ollama_server_config(logs)

    assert report["context_length"][0] == "4096"
    assert "VRAM-based" in report["context_length"][1]


def test_the_real_vllm_startup_line_from_ares_resolves_auto_to_bfloat16() -> None:
    from clio_agent.gact.infrastructure.effective_parameters import parse_vllm_engine_config

    line = (FIXTURES / "vllm_0.28.0_engine_config.log").read_text()

    report = parse_vllm_engine_config(line)

    assert report["dtype"][0] == "bfloat16"
    assert report["tensor_parallel_size"][0] == "1"
