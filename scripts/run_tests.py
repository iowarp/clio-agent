"""Run pytest with repository-contained state before loading any pytest plugins.

Use ``uv run --frozen --no-sync python -B scripts/run_tests.py <pytest arguments>``.
Set UV_CACHE_DIR inside the checkout before invoking uv when syncing dependencies.
"""

from __future__ import annotations

import os
import runpy
import sys
import time
from pathlib import Path

#: How long teardown waits for children that are already exiting (a log pump at EOF).
CHILD_EXIT_GRACE_S = 10.0


def active_children(grace_s: float = CHILD_EXIT_GRACE_S) -> list[str]:
    """Describe descendants still running after ``grace_s``; exited (zombie) ones are not.

    psutil reports a zombie as running, but it has exited and holds no state. It must
    not block cleanup, and reaping it here would steal a status its Popen still owns.
    """
    import psutil  # noqa: PLC0415

    deadline = time.monotonic() + grace_s
    while True:
        survivors: list[str] = []
        for child in psutil.Process().children(recursive=True):
            try:
                if child.status() == psutil.STATUS_ZOMBIE:
                    continue
                survivors.append(f"pid={child.pid} name={child.name()} cmd={child.cmdline()}")
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                survivors.append(f"pid={child.pid} ({exc.__class__.__name__})")
        if not survivors or time.monotonic() >= deadline:
            return survivors
        time.sleep(0.2)


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
                    "Test children remain active; state retained at "
                    f"{runtime.root}: " + "; ".join(survivors)
                )
            cleanup_test_runtime(runtime.root, runtime.parent)


if __name__ == "__main__":
    main()
