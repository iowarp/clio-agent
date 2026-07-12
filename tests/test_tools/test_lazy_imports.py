"""Import-time regression tests for lightweight CLIO entrypoints."""

from __future__ import annotations

import subprocess
import sys

# Cold Python interpreter start plus the probed import can take several seconds on
# its own; when the full suite runs in parallel (pytest-xdist / a loaded CI box) the
# spawned child contends for CPU and disk and a 10s ceiling was hit spuriously (#902).
# This bound is generous enough to absorb that contention yet still fails loud if an
# import genuinely hangs (e.g. platform._wmi_query on Windows) rather than merely
# being slow. It is a subprocess-cold-start margin, not a correctness threshold.
_IMPORT_PROBE_TIMEOUT_S = 60


def _run_import_probe(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=_IMPORT_PROBE_TIMEOUT_S,
    )


def test_file_policy_import_does_not_load_scientific_gateway() -> None:
    """Importing file policy must not import every scientific tool server."""

    result = _run_import_probe(
        "import sys; "
        "import clio_agent.tools.file_policy; "
        "assert 'clio_agent.tools.gateway' not in sys.modules; "
        "assert 'clio_agent.tools.servers.hdf5_server' not in sys.modules; "
        "assert 'h5py' not in sys.modules; "
        "print('ok')"
    )
    assert result.stdout.strip() == "ok"


def test_gact_app_import_with_cli_args_does_not_load_hdf5() -> None:
    """GACT health/capability startup should not depend on HDF5 import speed."""

    result = _run_import_probe(
        "import sys; "
        "sys.argv = ['clio-agent-gact', '--host', '127.0.0.1', '--port', '18199']; "
        "import clio_agent.gact.app; "
        "assert 'clio_agent.tools.servers.hdf5_server' not in sys.modules; "
        "assert 'h5py' not in sys.modules; "
        "print('ok')"
    )
    assert result.stdout.strip() == "ok"


def test_clio_windows_platform_hardening_reaches_openai_headers() -> None:
    """CLIO's Windows platform guard must cover OpenAI client headers.

    LiteLLM's OpenAI path builds telemetry headers before sending a request.
    On this workstation that path can otherwise block inside
    ``platform._wmi_query`` before any provider timeout applies.
    """

    result = _run_import_probe(
        "import clio_agent; "
        "from openai import OpenAI; "
        "client = OpenAI(api_key='test-key', base_url='http://127.0.0.1:1/v1'); "
        "assert client.default_headers.get('X-Stainless-OS'); "
        "print('ok')"
    )
    assert result.stdout.strip() == "ok"
