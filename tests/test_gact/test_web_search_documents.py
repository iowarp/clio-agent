"""CLIO Web Search ``documents`` option: slim (search-only) vs full image selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clio_agent.gact.infrastructure import web_search_apptainer as backend
from clio_agent.gact.infrastructure import web_search_service as service
from clio_agent.gact.infrastructure.drivers import build_driver_plan, service_definitions
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    ContainerRuntimeFact,
    InfrastructureTarget,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.web_search_service import (
    WEB_SEARCH_IMAGE,
    WEB_SEARCH_PINNED_IMAGE,
)

TARGET = InfrastructureTarget(id="node", label="Node", kind="ssh")
SLIM_DIGEST = "ab" * 32
SLIM_IMAGE = f"ghcr.io/iowarp/clio-web-search@sha256:{SLIM_DIGEST}"
GROBID_BIND = backend.GROBID_TMP


def facts(*runtimes: str, docker: bool = False) -> TargetFacts:
    return TargetFacts(
        target_id="node",
        label="Node",
        os="linux",
        arch="x86_64",
        docker_available=docker,
        docker_installed=docker,
        agent_data_root="/data/clio",
        hostname="node",
        container_runtimes=[
            ContainerRuntimeFact(name=name, installed=True, usable=True)  # type: ignore[arg-type]
            for name in runtimes
        ],
    )


def plan(action: str, configuration: dict[str, str], runtime: str = "apptainer") -> DriverPlan:
    return build_driver_plan(
        service_id="web_search",
        action=action,
        variant_id="container",
        configuration={"container_runtime": runtime, **configuration},
        facts=facts(runtime, docker=runtime == "docker"),
        target=TARGET if runtime == "apptainer" else None,
    )


def argv(spec: CommandSpec) -> str:
    return " ".join([spec.program, *spec.args])


@pytest.fixture
def slim_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate filling the one slim-digest constant."""

    monkeypatch.setattr(service, "WEB_SEARCH_SLIM_DIGEST", SLIM_DIGEST)


def _apptainer_pulls(install: DriverPlan) -> list[str]:
    return [argv(spec) for spec in install.commands if "clio-apptainer-pull" in argv(spec)]


# --- catalog ------------------------------------------------------------------------------


def test_catalog_offers_documents_off_by_default_with_a_short_explanation() -> None:
    row = next(r for r in service_definitions(facts("docker", docker=True)) if r.id == "web_search")
    field = next(field for field in row.configuration_fields if field.id == "documents")
    assert field.options == ["off", "on"]
    assert field.placeholder.startswith("off")
    assert "PDF/document conversion" in field.placeholder
    assert "larger, slower install" in field.placeholder


def test_the_slim_digest_constant_is_empty_or_a_sha256() -> None:
    digest = service.WEB_SEARCH_SLIM_DIGEST
    assert digest == "" or (len(digest) == 64 and set(digest) <= set("0123456789abcdef"))


def test_catalog_artifact_follows_the_default_image(slim_published: None) -> None:
    del slim_published
    row = next(r for r in service_definitions(facts("apptainer")) if r.id == "web_search")
    assert row.variants[0].artifact == SLIM_IMAGE


# --- full image stays the effective default until the slim digest is pinned ----------------


@pytest.mark.parametrize("documents", ["", "off", "on"])
def test_without_a_slim_digest_every_install_uses_the_full_image(
    monkeypatch: pytest.MonkeyPatch, documents: str
) -> None:
    monkeypatch.setattr(service, "WEB_SEARCH_SLIM_DIGEST", "")
    docker = plan("install", {"documents": documents}, runtime="docker")
    assert argv(docker.commands[0]) == f"docker pull {WEB_SEARCH_IMAGE}"
    assert docker.commands[-1].args[-1] == WEB_SEARCH_IMAGE
    apptainer = plan("install", {"documents": documents})
    assert all(WEB_SEARCH_PINNED_IMAGE in line for line in _apptainer_pulls(apptainer))
    assert GROBID_BIND in " ".join(apptainer.commands[-1].args)
    configuration = apptainer.configuration or {}
    assert configuration["image_variant"] == "full"
    assert configuration["documents"] == ("on" if documents == "on" else "off")


# --- once pinned, documents=off installs the slim image ------------------------------------


def test_documents_off_installs_the_slim_image_on_docker(slim_published: None) -> None:
    del slim_published
    install = plan("install", {}, runtime="docker")
    assert argv(install.commands[0]) == f"docker pull {SLIM_IMAGE}"
    assert install.commands[-1].args[-1] == SLIM_IMAGE
    assert (install.configuration or {})["image_variant"] == "slim"


def test_documents_on_keeps_the_full_image(slim_published: None) -> None:
    del slim_published
    docker = plan("install", {"documents": "on"}, runtime="docker")
    assert argv(docker.commands[0]) == f"docker pull {WEB_SEARCH_IMAGE}"
    apptainer = plan("install", {"documents": "on"})
    assert all(WEB_SEARCH_PINNED_IMAGE in line for line in _apptainer_pulls(apptainer))
    assert GROBID_BIND in " ".join(apptainer.commands[-1].args)
    assert (apptainer.configuration or {})["image_variant"] == "full"


def test_documents_off_installs_the_slim_sif_without_a_grobid_bind(slim_published: None) -> None:
    del slim_published
    install = plan("install", {"documents": "off"})
    pulls = _apptainer_pulls(install)
    assert len(pulls) == 2 and all(SLIM_IMAGE in line for line in pulls)
    assert GROBID_BIND not in " ".join(install.commands[-1].args)
    configuration = install.configuration or {}
    assert configuration["image_variant"] == "slim"
    assert configuration["documents"] == "off"
    # A later start runs the recorded slim image the same way.
    start = plan("start", configuration)
    assert GROBID_BIND not in " ".join(start.commands[-1].args)


def test_a_deployment_recorded_before_the_option_starts_as_full(slim_published: None) -> None:
    del slim_published
    start = plan("start", {})
    assert GROBID_BIND in " ".join(start.commands[-1].args)
    assert (start.configuration or {})["image_variant"] == "full"


def test_verify_is_a_search_query_without_document_services(slim_published: None) -> None:
    del slim_published
    for runtime in ("docker", "apptainer"):
        verify = plan("verify", {"image_variant": "slim"}, runtime=runtime)
        probe = verify.commands[-1]
        assert service.VERIFY_SCRIPT in probe.args
        assert service.VERIFY_QUERY in probe.args
        assert "grobid" not in argv(probe).lower()


def test_an_unknown_documents_value_is_refused() -> None:
    with pytest.raises(ValueError, match="documents must be 'on' or 'off'"):
        plan("install", {"documents": "maybe"}, runtime="docker")


def test_a_malformed_slim_digest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "WEB_SEARCH_SLIM_DIGEST", "not-a-digest")
    with pytest.raises(ValueError, match="WEB_SEARCH_SLIM_DIGEST"):
        plan("install", {}, runtime="docker")


# --- the Apptainer launcher serves either image --------------------------------------------


def test_the_launcher_starts_grobid_only_when_the_image_has_it() -> None:
    launcher = backend.LAUNCHER
    assert "[ ! -x /opt/grobid/grobid-service/bin/grobid-service ]" in launcher
    assert '"${CLIO_WEB_SEARCH_DOCUMENTS_ENABLED:-}" = false' in launcher
    before_grobid = launcher[: launcher.index("./grobid-service/bin/grobid-service")]
    assert 'if [ "$documents" = true ]; then' in before_grobid
    assert '"$searxng_pid" ${grobid_pid:-} "$valkey_pid"' in launcher


@pytest.mark.skipif(sys.platform == "win32", reason="Apptainer is Linux-only")
def test_the_launcher_stays_valid_posix_shell(tmp_path: Path) -> None:
    script = tmp_path / "entrypoint.sh"
    script.write_text(backend.LAUNCHER)
    subprocess.run(["sh", "-n", str(script)], check=True)
