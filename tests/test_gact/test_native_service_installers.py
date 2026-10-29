"""Native installation contracts and archive boundaries across supported hosts."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure.context_sizing.target_probe import parse_llama_devices
from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.native_archive import digest, extract
from clio_agent.gact.infrastructure.native_model_services import (
    native_model_plan,
    native_model_variants,
)
from clio_agent.gact.infrastructure.native_monitoring_hook import (
    check_mongod,
    mongo_platform,
    private,
)
from clio_agent.gact.infrastructure.probe import _amd_gpu


def host(os: str = "linux", accelerator: str = "none") -> TargetFacts:
    """Describe an execution host with uv and no container runtime."""
    return TargetFacts(
        target_id="local",
        label="Native",
        os=os,
        arch="x86_64",
        accelerator=accelerator,
        uv_available=True,
        hostname="native",
        home="C:\\Users\\Alice" if os == "windows" else "/home/alice",
        agent_data_root="C:\\Native" if os == "windows" else "/data/clio",
    )


def request(plan: Any) -> dict[str, Any]:
    """Read the real supervisor's transmitted action for contract assertions."""
    return json.loads(plan.commands[-1].stdin)


@pytest.mark.parametrize("service", ["llama_cpp", "ollama", "web_search", "flowcept", "cmf"])
def test_native_catalog_and_driver_do_not_require_a_container_engine(service: str) -> None:
    facts = host()
    definition = next(row for row in service_definitions(facts) if row.id == service)
    native = next(row for row in definition.variants if row.id == definition.recommended_variant)
    assert native.compatible and native.install_type == "native_uv"
    plan = build_driver_plan(
        service_id=service,
        action="install",
        variant_id=native.id,
        configuration={},
        facts=facts,
        target=InfrastructureTarget(id="local", label="Native", kind="local"),
    )
    manifest = request(plan)["manifest"]
    assert "container_runtime" not in manifest
    assert all(
        command.program not in {"docker", "podman", "apptainer"} for command in plan.commands
    )
    assert plan.readiness and plan.readiness.capability == "installed"


def test_windows_amd_defaults_to_real_pinned_vulkan_with_rocm_alternative() -> None:
    facts = host("windows", "amd")
    definition = next(row for row in service_definitions(facts) if row.id == "llama_cpp")
    assert definition.recommended_variant == "native-windows-vulkan"
    assert next(row for row in definition.variants if row.id == "native-windows-rocm").compatible
    plan = native_model_plan(
        "llama_cpp",
        "clio-llama-cpp",
        "install",
        definition.recommended_variant,
        {},
        facts,
        None,
        None,
    )
    artifact = request(plan)["manifest"]["native_model"]["artifacts"][0]
    assert artifact["url"].endswith("llama-b11206-bin-win-vulkan-x64.zip")
    assert len(artifact["sha256"]) == 64


def test_late_model_port_and_explicit_zero_offload_do_not_change_installed_packages() -> None:
    facts = host("windows", "amd")
    installed = native_model_plan(
        "llama_cpp",
        "clio-llama-cpp",
        "install",
        "native-windows-vulkan",
        {},
        facts,
        None,
        "private-key",
    )
    configuration = {
        **installed.configuration,
        "model_path": "C:\\Models\\a model.gguf",
        "port": "18123",
        "param.gpu_layers": "0",
    }
    started = native_model_plan(
        "llama_cpp",
        "clio-llama-cpp",
        "start",
        "native-windows-vulkan",
        configuration,
        facts,
        None,
        "private-key",
    )
    before, after = request(installed)["manifest"], request(started)["manifest"]
    for manifest in (before, after):
        for field in manifest["runtime_fields"]:
            manifest.pop(field)
    assert before == after
    launch = json.loads(request(started)["runtime_files"]["runtime.json"])
    index = launch["flags"].index("--n-gpu-layers")
    assert launch["flags"][index + 1] == "0"
    assert "999" not in launch["flags"]
    assert "private-key" not in json.dumps(after)
    assert "C:\\Models\\a model.gguf" in launch["flags"]


@pytest.mark.parametrize(
    ("arch", "backend", "asset"),
    [
        ("x86_64", "cpu", "ubuntu-x64.tar.gz"),
        ("aarch64", "cpu", "ubuntu-arm64.tar.gz"),
        ("x86_64", "vulkan", "ubuntu-vulkan-x64.tar.gz"),
        ("x86_64", "cuda", "ubuntu-cuda-12.8-x64.tar.gz"),
    ],
)
def test_linux_llama_installs_upstream_prebuilt_binaries(
    arch: str, backend: str, asset: str
) -> None:
    accelerator = {"vulkan": "nvidia", "cuda": "nvidia"}.get(backend, "none")
    facts = host(accelerator=accelerator).model_copy(update={"arch": arch})
    plan = native_model_plan(
        "llama_cpp", "clio-llama-cpp", "install", f"native-linux-{backend}", {}, facts, None, None
    )
    manifest = request(plan)["manifest"]
    spec = manifest["native_model"]
    assert "source" not in spec
    assert spec["artifacts"][0]["url"].endswith(f"/llama-b11206-bin-{asset}")
    assert all(len(artifact["sha256"]) == 64 for artifact in spec["artifacts"])
    assert "cmake" not in manifest["project"] and "ninja" not in manifest["project"]
    runtime = [a["url"] for a in spec["artifacts"][1:]]
    if backend == "cuda":  # the CUDA 12 runtime it links comes from NVIDIA's own wheels
        assert any("nvidia_cuda_runtime_cu12" in url for url in runtime)
        assert any("nvidia_cublas_cu12" in url for url in runtime)
    else:
        assert runtime == []


def test_linux_rocm_llama_has_no_prebuilt_build_and_points_to_containers() -> None:
    facts = host(accelerator="amd")
    variants = {row.id: row for row in native_model_variants("llama_cpp", facts)}
    rocm = variants["native-linux-rocm"]
    assert not rocm.compatible and "container variant" in rocm.reason
    assert variants["native-linux-vulkan"].compatible


def test_no_native_installer_compiles_on_the_host() -> None:
    """Native installs fetch prebuilt binaries; a host they do not run on uses containers."""
    from clio_agent.gact.infrastructure import native_model_hook, native_monitoring_hook

    for module in (native_model_hook, native_monitoring_hook):
        text = Path(module.__file__).read_text(encoding="utf-8")
        for tool in ('"make"', '"cmake"', '"configure"', "/configure", '"ninja"', "c++"):
            assert tool not in text, f"{module.__name__} invokes {tool}"


def test_a_windows_binary_missing_its_runtime_dll_names_the_redistributable() -> None:
    import subprocess

    from clio_agent.gact.infrastructure.native_model_hook import refusal_detail

    silent = subprocess.CalledProcessError(0xC0000135, ["llama-server.exe"], "", "")
    assert "Visual C++ 2015-2022 Redistributable" in refusal_detail(silent)
    loader = subprocess.CalledProcessError(127, ["llama-server"], "", "libgomp.so.1: not found")
    assert refusal_detail(loader) == "libgomp.so.1: not found"


def test_a_prebuilt_database_that_cannot_run_is_refused_toward_containers(tmp_path: Path) -> None:
    from clio_agent.gact.infrastructure.native_monitoring_hook import check_runs

    binary = tmp_path / "valkey-server"
    binary.write_text(
        "#!/bin/sh\necho \"valkey-server: /lib64/libc.so.6: version 'GLIBC_2.34' not found\" >&2\n"
        "exit 1\n"
    )
    binary.chmod(0o755)
    with pytest.raises(RuntimeError, match="GLIBC_2.34") as refused:
        check_runs(binary, "Valkey's prebuilt Linux build")
    assert "does not compile" in str(refused.value)
    assert "container variant" in str(refused.value)


def test_linux_amd_is_detected_from_pci_without_rocm_tools(tmp_path: Path) -> None:
    vendor = tmp_path / "card0/device/vendor"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("0x1002\n", encoding="ascii")
    assert _amd_gpu(str(tmp_path))
    vendor.write_text("0x8086\n", encoding="ascii")
    assert not _amd_gpu(str(tmp_path))


@pytest.mark.parametrize("name", ["../escape", "/outside", "C:/outside", r"..\escape"])
def test_native_zip_refuses_host_path_escape(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, b"unsafe")
    with pytest.raises(ValueError, match="escapes"):
        extract(archive, tmp_path / "staging")


def test_native_archive_normalizes_posix_members_under_a_windows_style_root(tmp_path: Path) -> None:
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(r"runtime\bin\server.exe", b"real bytes")
    staging = tmp_path / "folder with spaces"
    extract(archive, staging)
    assert (staging / "runtime/bin/server.exe").read_bytes() == b"real bytes"
    assert len(digest(archive)) == 64


def test_native_tar_resolves_link_chains_independently_of_member_order(tmp_path: Path) -> None:
    archive = tmp_path / "download.tar"
    with tarfile.open(archive, "w") as bundle:
        for name, target in [("lib/a.so", "lib/a.so.1"), ("lib/a.so.1", "lib/a.so.2")]:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.LNKTYPE, target
            bundle.addfile(member)
        member = tarfile.TarInfo("lib/a.so.2")
        member.size = 4
        bundle.addfile(member, io.BytesIO(b"data"))
    staging = tmp_path / "staging"
    extract(archive, staging)
    assert (staging / "lib/a.so").read_bytes() == b"data"
    assert (staging / "lib/a.so").samefile(staging / "lib/a.so.2")


def test_native_tar_refuses_a_link_outside_its_owned_directory(tmp_path: Path) -> None:
    archive = tmp_path / "download.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("lib/link")
        member.type, member.linkname = tarfile.SYMTYPE, "../../escape"
        bundle.addfile(member)
    with pytest.raises(ValueError, match="escapes"):
        extract(archive, tmp_path / "staging")


def test_site_databases_require_a_host_private_settings_path() -> None:
    with pytest.raises(ValueError, match="absolute private"):
        build_driver_plan(
            service_id="flowcept",
            action="install",
            variant_id="native",
            configuration={"database_mode": "site", "database_settings_path": "relative.json"},
            facts=host(),
            target=InfrastructureTarget(id="local", label="Native", kind="local"),
        )


def test_database_mode_switch_preserves_managed_passwords(tmp_path: Path) -> None:
    manifest = {
        "database_mode": "managed",
        "service": "flowcept",
        "redis_port": 16379,
        "mongo_port": 37017,
        "postgres_port": 15432,
    }
    first = private(tmp_path, manifest)
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"redis_url": "redis://site:6379", "mongodb_uri": "mongodb://site"}))
    site.chmod(0o600)
    private(tmp_path, {**manifest, "database_mode": "site", "database_settings_path": str(site)})
    assert private(tmp_path, manifest) == first
    moved = private(
        tmp_path, {**manifest, "redis_port": 16380, "mongo_port": 37018, "postgres_port": 15433}
    )
    assert moved["password"] == first["password"]
    assert ":16380/" in moved["redis_url"] and ":37018/" in moved["mongodb_uri"]
    assert moved["POSTGRES_PORT"] == "15433"


@pytest.mark.parametrize(
    "release,platform",
    [
        ('ID=ubuntu\nVERSION_ID="24.04"', "ubuntu2404"),
        ('ID=debian\nVERSION_ID="12"', "debian12"),
        ('ID=rocky\nVERSION_ID="8.10"', "rhel8"),
        ('NAME="SLES"\nVERSION="15-SP5"\nID="sles"\nVERSION_ID="15.5"', "suse15"),
        ('ID="sle_hpc"\nVERSION_ID="15.4"', "suse15"),
        ('ID="opensuse-leap"\nVERSION_ID="15.6"', "suse15"),
    ],
)
def test_native_mongo_chooses_host_libc_build(release: str, platform: str) -> None:
    assert mongo_platform(release) == platform


def test_mongod_check_names_the_missing_library(tmp_path: Path) -> None:
    mongod = tmp_path / "mongod"
    mongod.write_text(
        "#!/bin/sh\necho 'mongod: error while loading shared libraries: libssl.so.1.1: "
        "cannot open shared object file' >&2\nexit 127\n"
    )
    mongod.chmod(0o755)
    with pytest.raises(RuntimeError, match="libopenssl1_1") as refused:
        check_mongod(mongod, "suse15")
    assert "libssl.so.1.1" in str(refused.value) and "container variant" in str(refused.value)
    mongod.write_text("#!/bin/sh\necho 'db version v8.0.14'\n")
    check_mongod(mongod, "suse15")


def test_native_mongo_refuses_unknown_distribution() -> None:
    with pytest.raises(RuntimeError, match="container variant"):
        mongo_platform('ID=unknown\nVERSION_ID="99"')
    with pytest.raises(RuntimeError, match="container variant"):
        mongo_platform('ID="sles"\nVERSION_ID="12.5"')


def test_native_llama_device_memory_and_source_mtimes(tmp_path: Path) -> None:
    assert parse_llama_devices("Vulkan0: AMD Radeon (16384 MiB, 14200 MiB free)") == [
        {"total": 16384 << 20, "free": 14200 << 20}
    ]
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as bundle:
        for name, stamp in [("configure.ac", 1_000_000), ("Makefile.in", 1_000_001)]:
            member = tarfile.TarInfo(name)
            member.size, member.mtime = 4, stamp
            bundle.addfile(member, io.BytesIO(b"data"))
    staging = tmp_path / "staging"
    extract(archive, staging)
    assert (staging / "configure.ac").stat().st_mtime < (staging / "Makefile.in").stat().st_mtime


def test_native_model_honors_shared_cache_without_transferring_delete_ownership() -> None:
    plan = native_model_plan(
        "ollama",
        "clio-ollama",
        "start",
        "native-linux-cpu",
        {"storage.model_cache": "/scratch/models/ollama"},
        host(),
        None,
        None,
    )
    assert plan.configuration["storage.model_cache"] == "/scratch/models/ollama"
    assert (
        json.loads(request(plan)["runtime_files"]["runtime.json"])["model_cache"]
        == "/scratch/models/ollama"
    )


def test_native_monitoring_exposes_real_producer_settings_and_capture_directory() -> None:
    plan = build_driver_plan(
        service_id="flowcept",
        action="install",
        variant_id="native",
        configuration={},
        facts=host(),
        target=InfrastructureTarget(id="local", label="Native", kind="local"),
    )
    assert plan.configuration["flowcept_settings"].endswith("/settings.yaml")
    assert plan.configuration["persistence_owner"] == "collector"
    assert plan.configuration["storage.captures"].endswith("/evidence")
