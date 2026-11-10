"""Run pytest with repository-contained state before loading any pytest plugins.

Use ``uv run --frozen --no-sync python -B scripts/run_tests.py <pytest arguments>``.
Set UV_CACHE_DIR inside the checkout before invoking uv when syncing dependencies.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def active_children() -> list[str]:
    """Describe this process's remaining descendants for the teardown error.

    An exited but unreaped child (zombie) is listed too: its spawner leaked it.
    """
    import psutil  # noqa: PLC0415

    survivors: list[str] = []
    for child in psutil.Process().children(recursive=True):
        try:
            if not child.is_running():
                continue
            survivors.append(f"pid={child.pid} status={child.status()} cmd={child.cmdline()}")
        except psutil.ZombieProcess:
            survivors.append(f"pid={child.pid} status=zombie (exited, never reaped)")
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            survivors.append(f"pid={child.pid} ({exc.__class__.__name__})")
    return survivors


def main() -> None:
    """Bootstrap the suite's owned runtime, then enter the normal pytest CLI."""
    sys.dont_write_bytecode = True
    checkout = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(checkout))
    from tests._test_runtime_isolation import (  # noqa: PLC0415
        bootstrap_test_runtime,
        cleanup_test_runtime,
    )

    runtime = bootstrap_test_runtime(checkout)
    os.environ["CLIO_TEST_CONTROLLER_RUNTIME"] = str(runtime.root)
    sys.argv[0] = "pytest"
    # pytest's cache provider loads before the suite's configure hook. Keep its
    # writes in the same disposable run even when an inherited option names a
    # different cache directory.
    sys.argv.extend(["-o", f"cache_dir={runtime.cache_dir / 'pytest'}"])
    try:
        runpy.run_module("pytest", run_name="__main__", alter_sys=True)
    finally:
        # Help/configuration failures may never enter the suite's session-finish hook.
        # Do not delete any remaining state while child custody is unresolved.
        if runtime.root.exists():
            survivors = active_children()
            if survivors:
                raise RuntimeError(
                    f"Test children remain active; state retained at {runtime.root}: "
                    + "; ".join(survivors)
                )
            cleanup_test_runtime(runtime.root, runtime.parent)


if __name__ == "__main__":
    main()
