"""Remove files a bundled runtime never reads, shared by every OS's build.

Each file in the runtime is written, malware-scanned and, on macOS, signed on
every install. Windows Defender scans about 380 files a second during the unpack
(gact-tui#739), so every 1,000 unread files cost a Windows install about 3 s.

What goes:

* pip, which nothing runs: every installer in CLIO uses the ``uv`` shipped beside
  the interpreter (providers/dependencies.py, providers/components/updater.py,
  runtime/document_runtime.py), and ``python -m venv`` still has ``ensurepip``.
* C, C++ and Cython sources and headers, read only when compiling against a
  package. cffi keeps its headers, because cffi compiles extensions while it runs,
  and so does iowarp_core.
* The ``INSTALLER`` and ``REQUESTED`` markers in each ``.dist-info``. Neither
  importlib.metadata nor uv needs them to import, upgrade or uninstall a package.
* ``globus_sdk.testing``, response fixtures for a caller's own test suite.
* botocore's API models for AWS services CLIO never calls: 1,825 of its 1,926 data
  files in 0.9.6. botocore reads ``botocore/data/<service>`` only when a client for
  that service is created. Only litellm's Bedrock and SageMaker paths and botocore's
  own credential providers create clients, for the ``BOTOCORE_SERVICES`` below.
  Signing a request (SigV4) needs no model, and nor does ``host_credentials``'s
  offline credential check.

What stays: ``RECORD``, which uv needs to upgrade the runtime in place,
``direct_url.json`` and ``METADATA``, license files (redistribution requires them),
and type stubs, which globus_sdk and griffe read when they run.

Usage: ``prune_runtime.py <site-packages>``
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

BUILD_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".h", ".hpp", ".pxd", ".pxi", ".pyx"})
KEEPS_BUILD_SOURCES = frozenset({"cffi", "iowarp_core"})
UNREAD_DIST_INFO_FILES = frozenset({"INSTALLER", "REQUESTED"})
UNREAD_TREES = ("pip", "globus_sdk/testing")
# Every service litellm, boto3, s3transfer or botocore's credential chain names
# (sts, sso, sso-oidc and signin resolve credentials). A client for any other
# service raises botocore's UnknownServiceError.
BOTOCORE_SERVICES = frozenset(
    {
        "bedrock",
        "bedrock-agent",
        "bedrock-agent-runtime",
        "bedrock-agentcore",
        "bedrock-runtime",
        "cloudwatch",
        "dynamodb",
        "iam",
        "kms",
        "logs",
        "polly",
        "s3",
        "sagemaker",
        "sagemaker-runtime",
        "secretsmanager",
        "signin",
        "sqs",
        "sso",
        "sso-oidc",
        "sts",
        "transcribe",
    }
)


def _count_files(path: Path) -> int:
    return sum(1 for entry in path.rglob("*") if entry.is_file())


def _remove_empty_dirs(root: Path) -> None:
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def prune(site: Path) -> dict[str, int]:
    """Delete the unread files under ``site`` and return how many went, by kind."""
    removed = {
        "pip and unread trees": 0,
        "build sources": 0,
        "dist-info markers": 0,
        "AWS service models": 0,
    }

    trees = [site / tree for tree in UNREAD_TREES]
    trees += sorted(site.glob("pip-*.dist-info"))
    for tree in trees:
        if tree.is_dir():
            removed["pip and unread trees"] += _count_files(tree)
            shutil.rmtree(tree)

    for package in sorted(p for p in site.iterdir() if p.is_dir()):
        if package.name in KEEPS_BUILD_SOURCES or package.name.endswith(".dist-info"):
            continue
        touched = False
        for path in package.rglob("*"):
            if path.suffix.lower() in BUILD_SOURCE_SUFFIXES and path.is_file():
                path.unlink()
                removed["build sources"] += 1
                touched = True
        if touched:
            _remove_empty_dirs(package)

    for dist_info in site.glob("*.dist-info"):
        for name in UNREAD_DIST_INFO_FILES:
            marker = dist_info / name
            if marker.is_file():
                marker.unlink()
                removed["dist-info markers"] += 1

    # The root files (endpoints.json, partitions.json, _retry.json, ...) stay.
    models = site / "botocore" / "data"
    if models.is_dir():
        for service in sorted(p for p in models.iterdir() if p.is_dir()):
            if service.name not in BOTOCORE_SERVICES:
                removed["AWS service models"] += _count_files(service)
                shutil.rmtree(service)
    return removed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: prune_runtime.py <site-packages>")
    site = Path(argv[1])
    if not site.is_dir():
        raise SystemExit(f"prune_runtime: {site} is not a directory")
    removed = prune(site)
    if (site / "pip").exists():
        raise SystemExit("prune_runtime: pip is still installed")
    models = site / "botocore" / "data"
    if models.is_dir() and not (models / "sts").is_dir():
        raise SystemExit("prune_runtime: botocore has no sts model to keep")
    detail = ", ".join(f"{kind} {count}" for kind, count in removed.items())
    print(f"[prune_runtime] removed {sum(removed.values())} unread files ({detail})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
