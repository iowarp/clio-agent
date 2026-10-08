"""Pinned native vLLM deployments using the shared target supervisor and ledger."""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Sequence

from clio_agent.gact.infrastructure.models import (
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.server_parameter_defaults import parser_defaults
from clio_agent.gact.infrastructure.server_parameters import compile_parameters
from clio_agent.gact.infrastructure.supervised_service import supervised_plan

CONNECTOR_REVISION = "95ab2acd6fe1be74ad9a3fa2aca1ecbee60a6284"
FLOWCEPT_REVISION = "e638b4e2072290a2921965a03a150db124e11c2e"
ATTENTION_PROFILE = "vllm-0.27.0-attention-1"
NATIVE_VARIANTS = frozenset({"native-cuda", "native-cuda-attention"})


def served_model(model: str, flags: Sequence[str]) -> str:
    """The model id vLLM lists: ``--served-model-name`` when set, else the model path."""
    for index, flag in enumerate(flags):
        if flag == "--served-model-name" and index + 1 < len(flags):
            return flags[index + 1]
        if flag.startswith("--served-model-name="):
            return flag.split("=", 1)[1]
    return model


def native_variants(facts: TargetFacts) -> list[ServiceVariant]:
    """Offer native CUDA only after the selected host actually reports a usable GPU."""
    compatible = (
        facts.os == "linux"
        and facts.arch in {"x86_64", "aarch64"}
        and facts.accelerator == "nvidia"
        and facts.uv_available
    )
    reason = "Requires Linux, a working NVIDIA GPU and uv on this execution host."
    return [
        ServiceVariant(
            id=variant,
            label="Native CUDA + attention" if variant.endswith("attention") else "Native CUDA",
            version="0.27.0" if variant.endswith("attention") else "0.28.0",
            install_type="native_uv",
            artifact=ATTENTION_PROFILE if variant.endswith("attention") else "vllm==0.28.0",
            compatible=compatible,
            reason="Ready for native installation." if compatible else reason,
        )
        for variant in ("native-cuda", "native-cuda-attention")
    ]


# vLLM binds ZMQ IPC sockets at $VLLM_RPC_BASE_PATH/<uuid4> (default: the temp dir). A deep
# service directory overflows the 107-byte AF_UNIX path limit, so use a private short directory.
IPC_PRELUDE = """import hashlib
import os
import tempfile

_base = os.environ.get("VLLM_RPC_BASE_PATH") or tempfile.gettempdir()
if len(_base) + 38 > 107:
    _base = "/tmp/clio-vllm-" + hashlib.sha256(_base.encode()).hexdigest()[:16]
    os.makedirs(_base, mode=0o700, exist_ok=True)
    _stat = os.stat(_base)
    if _stat.st_uid != os.getuid() or _stat.st_mode & 0o077:
        raise RuntimeError("The short vLLM socket directory is not private to this user")
    os.environ["VLLM_RPC_BASE_PATH"] = _base
"""


# vLLM may spawn its engine core; a spawned child re-imports this file as __mp_main__ and must
# not start a second API server.
RUN_GUARD = 'if __name__ == "__main__":\n'


def launcher(attention: bool) -> str:
    """Activate the pinned probe before importing or constructing vLLM's engine."""
    if not attention:
        return (
            IPC_PRELUDE
            + "import runpy\n"
            + RUN_GUARD
            + '    runpy.run_module("vllm.entrypoints.openai.api_server", run_name="__main__")\n'
        )
    return (
        IPC_PRELUDE
        + """import json
import runpy
import sys
from pathlib import Path

# Settings are a private file on this host, never agent/transcript material.
root = Path(__file__).parent
manifest = json.loads((root / "manifest.json").read_text())
settings = Path(manifest["flowcept_settings"])
if not settings.is_absolute() or not settings.is_file():
    raise RuntimeError("Choose the Flowcept settings file on this execution host")
os.environ["FLOWCEPT_SETTINGS_PATH"] = str(settings)
os.environ["VLLM_ENABLE_V1_MULTIPROCESSING"] = "0"
import vllm_attn_connector
if not vllm_attn_connector.install_probe():
    raise RuntimeError("The pinned attention probe could not be activated")
from flowcept import Flowcept
workflow = manifest["workflow_id"]
sys.argv.extend(["--kv-transfer-config", json.dumps({
    "kv_connector": "AttnConnector",
    "kv_connector_module_path": "vllm_attn_connector",
    "kv_role": "kv_producer",
    "kv_connector_extra_config": {"workflow_id": workflow, "out_dir": str(root / "evidence")},
})])
"""
        + RUN_GUARD
        + """    # The separately managed collector is the sole persistence owner.
    with Flowcept("vllm", workflow_id=workflow, workflow_name="CLIO attention", start_persistence=False):
        runpy.run_module("vllm.entrypoints.openai.api_server", run_name="__main__")
"""
    )


def native_vllm_plan(
    action: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    directory: str,
    port: int,
    api_key: str | None,
) -> DriverPlan:
    """Install packages, start serving, and retain evidence as separate operations."""
    if facts.os != "linux":
        raise ValueError("Native vLLM lifecycle requires its Linux execution host")
    if action in {"install", "reinstall", "start"} and not native_variants(facts)[0].compatible:
        raise ValueError(native_variants(facts)[0].reason)
    attention = variant_id == "native-cuda-attention"
    version = "0.27.0" if attention else "0.28.0"
    model = configuration.get("model", "").strip()
    if action in {"install", "reinstall", "start"} and not posixpath.isabs(model):
        raise ValueError("Select a downloaded model directory on this execution host")
    settings = configuration.get("flowcept_settings", "").strip()
    if attention and action in {"install", "reinstall", "start"} and not posixpath.isabs(settings):
        raise ValueError("Choose the managed Flowcept settings file before enabling attention")
    dependencies = [f"vllm=={version}"]
    if attention:
        dependencies.extend(
            [
                f"vllm-attn-connector @ git+https://github.com/spotter-ai-genesis/vllm-attn-connector.git@{CONNECTOR_REVISION}",
                f"flowcept[extras] @ git+https://github.com/spotter-ai-genesis/flowcept.git@{FLOWCEPT_REVISION}",
            ]
        )
    if action in {"install", "reinstall", "start"}:
        configuration = {**configuration, **parser_defaults(model, configuration)}
    compiled = compile_parameters("vllm", "cuda", configuration)
    ownership = hashlib.sha256(
        f"{facts.target_id}:{facts.hostname}:{directory}".encode()
    ).hexdigest()
    manifest = {
        "definition_version": ATTENTION_PROFILE if attention else "vllm-0.28.0-native-1",
        "project": '[project]\nname="clio-native-vllm"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps(dependencies)
        + "\n",
        "launcher": launcher(attention),
        "model_path": model,
        "model_revision": configuration.get("model_revision", ""),
        "port": port,
        "health_path": "/health",
        "identity": {"kind": "openai", "served_model": served_model(model, compiled.flags)},
        "arguments": [
            "--model",
            model,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            *compiled.flags,
        ],
        "environment": dict(compiled.env),
        "flowcept_settings": settings,
        "workflow_id": configuration.get("workflow_id") or f"clio-{ownership[:16]}",
    }
    resolved = {
        **configuration,
        "storage.service_directory": directory,
        "storage.captures": posixpath.join(directory, "evidence"),
        "compatibility_profile": str(manifest["definition_version"]),
        "native_owner": ownership,
    }
    return supervised_plan(
        action,
        directory=directory,
        ownership=ownership,
        manifest=manifest,
        port=port,
        label="vLLM",
        configuration=resolved,
        api_key=api_key,
    )
