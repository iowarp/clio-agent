"""The project ``.env`` file: CLIO's environment defaults kept outside Python code.

Commands like ``uv run src/clio_agent/ui/cli.py`` load it before reading any
configuration, so repo-local host and model defaults apply. It is a bootstrap
step, read before the configuration store exists (see :mod:`clio_agent.conf`).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_LOADED_KEY = "CLIO_ENV_FILE_LOADED"


def load_project_env_file(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
) -> Path | None:
    """Load CLIO environment defaults from a dotenv-style file.

    This keeps host/model defaults outside Python code while making direct
    commands like ``uv run src/clio_agent/ui/cli.py`` pick up the repo-local
    configuration. Existing process environment variables win unless
    ``override`` is true.
    """
    env_file = _resolve_env_file(path)
    if env_file is None or not env_file.exists():
        return None

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value

    os.environ[ENV_FILE_LOADED_KEY] = str(env_file)
    return env_file


def _resolve_env_file(path: str | os.PathLike[str] | None) -> Path | None:
    explicit = path or os.environ.get("CLIO_ENV_FILE", "")
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env.resolve(strict=False)

    repo_env = Path(__file__).resolve().parents[2] / ".env"
    if repo_env.exists():
        return repo_env.resolve(strict=False)
    return None


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").strip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value
