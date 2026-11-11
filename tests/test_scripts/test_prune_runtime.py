"""The runtime prune keeps only the AWS service models CLIO can create clients for."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _prune_module():
    script = Path(__file__).resolve().parents[2] / "install/prune_runtime.py"
    spec = importlib.util.spec_from_file_location("_prune_runtime_qualification", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prune_keeps_only_listed_botocore_service_models(tmp_path: Path) -> None:
    prune = _prune_module()
    data = tmp_path / "botocore" / "data"
    for service in ("sts", "bedrock-runtime", "sso-oidc", "ec2", "cloudfront"):
        model = data / service / "2020-01-01"
        model.mkdir(parents=True)
        (model / "service-2.json.gz").write_bytes(b"model")
        (model / "paginators-1.json").write_text("{}", encoding="utf-8")
    for name in ("endpoints.json", "partitions.json", "_retry.json"):
        (data / name).write_text("{}", encoding="utf-8")

    removed = prune.prune(tmp_path)

    assert removed["AWS service models"] == 4
    assert sorted(p.name for p in data.iterdir()) == [
        "_retry.json",
        "bedrock-runtime",
        "endpoints.json",
        "partitions.json",
        "sso-oidc",
        "sts",
    ]


def test_every_service_litellm_creates_a_client_for_is_kept() -> None:
    prune = _prune_module()
    # litellm's literal boto3 clients and botocore's credential providers.
    assert {"sts", "kms", "s3", "sagemaker-runtime", "sso", "sso-oidc", "signin"} <= (
        prune.BOTOCORE_SERVICES
    )
