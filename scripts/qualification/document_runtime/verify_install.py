"""Exercise installed tools through uv, pnpm and the real workspace shell."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

from clio_agent.runtime.document_runtime import STACK_ROOT, prepare_document_runtime
from clio_agent.tools.servers.shell_server import _shell_argv


def main() -> None:
    """Verify a provisioned execution host and create/render the four document formats."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--shell-only", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    result = prepare_document_runtime(root)
    environment = {**os.environ, **result["shell_environment"]}
    (root / "runtime-verified.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not args.shell_only:
        subprocess.run(
            [*result["python_argv"], str(STACK_ROOT / "smoke.py"), str(root / "smoke")],
            cwd=root,
            env=environment,
            check=True,
        )
    script = Path(result["javascript"]["workspace"]) / "verify-installed.cjs"
    script.write_text(
        "require('docx'); require('pptxgenjs'); require('sharp'); "
        "require('assert').strictEqual(require('os').tmpdir(), process.env.CLIO_DOCUMENT_SCRATCH); "
        "console.log('managed imports and workspace scratch ready');",
        encoding="utf-8",
    )
    subprocess.run(
        [*result["javascript"]["script_argv"], str(script)], cwd=root, env=environment, check=True
    )
    python = root / "verify-python.py"
    python.write_text(
        "import sys, tempfile, reportlab\n"
        "from pathlib import Path\n"
        "assert Path(tempfile.gettempdir()).is_relative_to(Path(__file__).parent / '.tmp')\n"
        "print(sys.executable)\nprint('Python workspace scratch ready')\n",
        encoding="utf-8",
    )
    commands = (
        [f'python "{python}"', "pnpm --version", "node --version"]
        if os.name == "nt"
        else [f"python {shlex.quote(str(python))}", "pnpm --version", "node --version"]
    )
    for command in commands:
        shell = _shell_argv(command, prepared_environment=result["shell_environment"])
        subprocess.run(shell, cwd=root, env=environment, check=True)


if __name__ == "__main__":
    main()
