"""Local native services build on CLIO's own Python; remote and Windows keep the pin."""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact.infrastructure.drivers import build_driver_plan
from clio_agent.gact.infrastructure.model_router import PORT_FIELD
from clio_agent.gact.infrastructure.model_router import ROUTER_SERVICE as SERVICE_ID
from clio_agent.gact.infrastructure.model_router import ROUTER_VARIANT as VARIANT_ID
from clio_agent.gact.infrastructure.models import InfrastructureTarget, TargetFacts
from clio_agent.gact.infrastructure.supervised_service import PINNED_PYTHON, service_python


def facts(**values: Any) -> TargetFacts:
    return TargetFacts.model_validate(
        {
            "target_id": "local",
            "label": "This computer",
            "os": "macos",
            "arch": "aarch64",
            "uv_available": True,
            "hostname": "mac",
            "agent_data_root": "/data/clio",
            **values,
        }
    )


def service_manifest(host: TargetFacts, kind: str = "local") -> dict[str, Any]:
    plan = build_driver_plan(
        service_id=SERVICE_ID,
        action="install",
        variant_id=VARIANT_ID,
        configuration={PORT_FIELD: "18800"},
        facts=host,
        target=InfrastructureTarget(id=host.target_id, label=host.label, kind=kind),
    )
    return json.loads(plan.commands[-1].stdin or "{}")["manifest"]


@pytest.mark.parametrize("os_name", ["macos", "linux"])
def test_a_local_service_uses_clio_python_with_a_widened_pin(os_name: str) -> None:
    assert service_python(facts(os=os_name)) == (">=3.12", {"interpreter": "clio"})
    manifest = service_manifest(facts(os=os_name))
    assert manifest["interpreter"] == "clio"
    assert 'requires-python=">=3.12"\n' in manifest["project"]


def test_remote_and_windows_services_keep_the_pinned_python() -> None:
    assert service_python(facts(target_id="hpc", label="HPC", os="linux")) == (PINNED_PYTHON, {})
    assert service_python(facts(os="windows", arch="x86_64")) == (PINNED_PYTHON, {})
    remote = service_manifest(facts(target_id="hpc", label="HPC", os="linux"), kind="ssh")
    assert "interpreter" not in remote
    assert f'requires-python="{PINNED_PYTHON}"' in remote["project"]
