"""In-place update of one provider's SDK components, with typed stages and rollback.

Stages (``UpdateJob.stage``): ``checking`` -> ``downloading`` -> ``installing``
-> ``verifying`` -> ``done`` | ``failed``. The mechanics reuse what the desktop
"Update all" flow learned (the stale-bytecode incident, #1414):

* **checking** asks PyPI again (no cache) and, on Windows, refuses up front
  (``component_in_use``) when a binary the update must replace is running --
  Windows cannot overwrite a running ``.exe``, and a half-uninstalled package is
  worse than no update.
* **downloading** fetches the target wheels AND the currently installed
  versions' wheels (sha256-verified) before anything changes, so a rollback
  never depends on the network. A current version with no downloadable wheel
  refuses the update (``rollback_unavailable``).
* **installing** runs the runtime's own installer (``uv pip install``, else
  pip) with ``--reinstall-package`` for the group and a constraints file that
  pins EVERY other installed distribution to its present version -- the
  runtime's lock-derived set is kept; a new version that needs a different
  dependency version fails resolution (``install_failed``) before any file
  changes. The group's stale ``__pycache__`` is then removed and the packages
  recompiled CHECKED_HASH, exactly as the bundle build compiles them.
* **verifying** starts a FRESH interpreter that must import every module and
  see exactly the target versions (one dist-info each), then re-runs the
  provider's own check and model discovery there
  (:mod:`clio_agent.providers.components.verify`).

Any failure after files changed reinstalls the downloaded previous wheels the
same way and reports ``rolled_back`` (or ``rollback_failed``). A module the
running process already imported keeps its old code until restart, reported as
``restart_required`` rather than hot-reloaded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from clio_agent.providers.components.pypi import RELEASES, ReleaseLookup, WheelFile
from clio_agent.providers.components.registry import ProviderComponents, components_for

logger = logging.getLogger(__name__)

UpdateStage = Literal["checking", "downloading", "installing", "verifying", "done", "failed"]

_INSTALL_TIMEOUT_S = 600.0
_DOWNLOAD_TIMEOUT_S = 120.0
_TAIL_CHARS = 1200


class UpdateFailed(RuntimeError):
    """A typed update failure (``code`` is the queryable reason)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UpdateInProgressError(RuntimeError):
    """Refusal to start a second update while one is running."""


@dataclass
class UpdateJob:
    """The observable state of one update (served by the update-status route)."""

    provider_kind: str
    stage: UpdateStage = "checking"
    from_versions: dict[str, str] = field(default_factory=dict)
    to_versions: dict[str, str] = field(default_factory=dict)
    changed: bool = False
    rolled_back: bool = False
    restart_required: bool = False
    error_code: str = ""
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""

    @property
    def running(self) -> bool:
        """Whether the job has not reached a terminal stage."""
        return self.stage not in {"done", "failed"}

    def to_wire(self) -> dict[str, Any]:
        """JSON shape of the job."""
        return {
            "provider_kind": self.provider_kind,
            "stage": self.stage,
            "running": self.running,
            "from_versions": dict(self.from_versions),
            "to_versions": dict(self.to_versions),
            "changed": self.changed,
            "rolled_back": self.rolled_back,
            "restart_required": self.restart_required,
            "error": {"code": self.error_code, "message": self.error} if self.error_code else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class VerifyOutcome:
    """The fresh-interpreter provider check's answer."""

    ok: bool
    code: str = ""
    detail: str = ""


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one installer/verification command without a console window."""
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(  # noqa: S603 - argv built from CLIO's own paths and PyPI wheels
        command,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
        check=False,
        **kwargs,
    )


def download_wheel(wheel: WheelFile, directory: Path) -> Path:
    """Download one wheel into ``directory`` and verify its sha256 (``file://`` for local mirrors)."""
    target = directory / wheel.filename
    digest = hashlib.sha256()
    parsed = urllib.parse.urlparse(wheel.url)
    try:
        if parsed.scheme == "file":
            source = Path(urllib.request.url2pathname(parsed.path))
            with source.open("rb") as src, target.open("wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    digest.update(chunk)
                    dst.write(chunk)
        else:
            with httpx.stream(
                "GET", wheel.url, timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True
            ) as response:
                if response.status_code != 200:
                    raise UpdateFailed(
                        "download_failed", f"{wheel.filename}: HTTP {response.status_code}"
                    )
                with target.open("wb") as dst:
                    for chunk in response.iter_bytes():
                        digest.update(chunk)
                        dst.write(chunk)
    except (OSError, httpx.HTTPError) as exc:
        raise UpdateFailed("download_failed", f"{wheel.filename}: {exc}") from exc
    if wheel.sha256 and digest.hexdigest() != wheel.sha256:
        raise UpdateFailed(
            "download_hash_mismatch", f"{wheel.filename}: sha256 does not match the index"
        )
    return target


def _tail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "no diagnostic").strip()[-_TAIL_CHARS:]


# --- scripts run inside the TARGET interpreter (standard library only) ------

_FREEZE_SCRIPT = (
    "import importlib.metadata as m, json; "
    "print(json.dumps(sorted({(d.metadata['Name'] or '').lower().replace('_','-'): d.version "
    "for d in m.distributions() if d.metadata['Name']}.items())))"
)

_IN_USE_SCRIPT = (
    "import importlib.metadata as m, json, sys; out=[]\n"
    "for name in json.loads(sys.argv[1]):\n"
    "    try: files = m.distribution(name).files or []\n"
    "    except m.PackageNotFoundError: continue\n"
    "    for f in files:\n"
    "        if str(f).lower().endswith(('.exe', '.dll', '.pyd')):\n"
    "            p = f.locate()\n"
    "            try:\n"
    "                with open(p, 'r+b'): pass\n"
    "            except PermissionError: out.append(str(p))\n"
    "            except OSError: pass\n"
    "print(json.dumps(out))"
)

_RECOMPILE_SCRIPT = (
    "import compileall, importlib.util, json, py_compile, shutil, sys\n"
    "from pathlib import Path\n"
    "sys.pycache_prefix = None  # next to the sources, where the bundle build puts them\n"
    "ok = True\n"
    "for mod in json.loads(sys.argv[1]):\n"
    "    spec = importlib.util.find_spec(mod)\n"
    "    for loc in (spec.submodule_search_locations or []) if spec else []:\n"
    "        for cache in list(Path(loc).rglob('__pycache__')): shutil.rmtree(cache, ignore_errors=True)\n"
    "        ok = compileall.compile_dir(loc, quiet=1, "
    "invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH) and ok\n"
    "sys.exit(0 if ok else 4)"
)

_VERSION_SCRIPT = (
    "import importlib, importlib.metadata as m, json, sys\n"
    "expected = json.loads(sys.argv[1]); modules = json.loads(sys.argv[2])\n"
    "found = {n: sorted({d.version for d in m.distributions(name=n)}) for n in expected}\n"
    "for mod in modules: importlib.import_module(mod)\n"
    "print(json.dumps(found))\n"
    "sys.exit(0 if all(found[n] == [v] for n, v in expected.items()) else 3)"
)


@dataclass
class UpdateEnvironment:
    """Everything an update touches, injectable so tests drive a scratch venv.

    Attributes:
        python: The interpreter whose environment is updated.
        lookup: Release lookup (PyPI JSON by default).
        run: Command runner.
        download: Wheel downloader.
        verify_provider: The fresh-interpreter provider check, or ``None`` to
            skip it (only for environments without CLIO installed).
        release_runtimes: Stops in-process runtimes holding the group's binaries.
        spec: Override of the provider's component group (tests).
        record_support: Records a finished update's versions as the floor the
            next runtime must keep (:func:`clio_agent.providers.support_record.
            record_support`); ``None`` for an environment that is not CLIO's own.
    """

    python: str = field(default_factory=lambda: sys.executable)
    lookup: ReleaseLookup = field(default_factory=lambda: RELEASES)
    run: Runner = run_command
    download: Callable[[WheelFile, Path], Path] = download_wheel
    verify_provider: Callable[[str, str], VerifyOutcome] | None = None
    release_runtimes: Callable[[str], None] | None = None
    spec: ProviderComponents | None = None
    record_support: Callable[[str, dict[str, str]], object] | None = None


def verify_provider_in_child(python: str, provider_kind: str) -> VerifyOutcome:
    """Run ``python -m clio_agent.providers.components.verify <kind>`` and parse its JSON line."""
    result = run_command([python, "-m", "clio_agent.providers.components.verify", provider_kind])
    lines = [line for line in (result.stdout or "").splitlines() if line.strip().startswith("{")]
    try:
        payload = json.loads(lines[-1]) if lines else {}
    except ValueError:
        payload = {}
    if result.returncode == 0 and payload.get("ok") is True:
        return VerifyOutcome(True, str(payload.get("code") or ""), str(payload.get("detail") or ""))
    return VerifyOutcome(
        False,
        str(payload.get("code") or "provider_check_failed"),
        str(payload.get("detail") or _tail(result)),
    )


def _uv_for(python: str) -> str | None:
    uv_name = "uv.exe" if os.name == "nt" else "uv"
    runtime_uv = Path(python).resolve().parent.parent / "bin" / uv_name
    if runtime_uv.is_file():
        return str(runtime_uv)
    return shutil.which(uv_name)


def install_command(
    python: str, constraints: Path, distributions: tuple[str, ...], wheels: list[Path]
) -> list[str]:
    """The installer argv: uv when available (the runtime's own), else pip."""
    uv = _uv_for(python)
    if uv:
        command = [uv, "pip", "install", "--python", python, "--constraint", str(constraints)]
        for name in distributions:
            command += ["--reinstall-package", name]
        return [*command, *map(str, wheels)]
    return [
        python,
        "-m",
        "pip",
        "install",
        "--constraint",
        str(constraints),
        "--force-reinstall",
        *map(str, wheels),
    ]


class ComponentUpdater:
    """One update at a time per runtime; the last job per provider stays readable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, UpdateJob] = {}
        self._busy = False

    def job(self, provider_kind: str) -> UpdateJob | None:
        """The latest job for ``provider_kind`` (running or finished)."""
        with self._lock:
            return self._jobs.get(provider_kind)

    def start(
        self,
        provider_kind: str,
        env: UpdateEnvironment,
        on_finish: Callable[[UpdateJob], None] | None = None,
    ) -> UpdateJob:
        """Start an update on a worker thread and return its (running) job."""
        job = self._claim(provider_kind)

        def _work() -> None:
            self._execute(job, env)
            if on_finish is not None:
                try:
                    on_finish(job)
                except Exception:  # noqa: BLE001 - a post-update hook must not hide the result
                    logger.exception(
                        "component update on_finish hook failed reason=component_update_hook_failed"
                    )

        threading.Thread(
            target=_work, name=f"component-update-{provider_kind}", daemon=True
        ).start()
        return job

    def run(self, provider_kind: str, env: UpdateEnvironment) -> UpdateJob:
        """Run an update to completion on the calling thread."""
        job = self._claim(provider_kind)
        self._execute(job, env)
        return job

    def _claim(self, provider_kind: str) -> UpdateJob:
        with self._lock:
            if self._busy:
                raise UpdateInProgressError("a provider component update is already running")
            self._busy = True
            job = UpdateJob(provider_kind=provider_kind)
            self._jobs[provider_kind] = job
            return job

    def _execute(self, job: UpdateJob, env: UpdateEnvironment) -> None:
        try:
            _perform(job, env)
            if job.changed and env.record_support is not None:
                env.record_support(job.provider_kind, dict(job.to_versions))
            job.stage = "done"
        except UpdateFailed as exc:
            job.error_code, job.error = exc.code, str(exc)
            job.stage = "failed"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a typed job state
            logger.exception("component update crashed reason=component_update_crashed")
            job.error_code, job.error = "component_update_crashed", repr(exc)
            job.stage = "failed"
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "component update finished provider=%s stage=%s from=%s to=%s rolled_back=%s error=%s",
                job.provider_kind,
                job.stage,
                job.from_versions,
                job.to_versions,
                job.rolled_back,
                job.error_code,
            )
            with self._lock:
                self._busy = False


def _check(
    job: UpdateJob, env: UpdateEnvironment, spec: ProviderComponents
) -> dict[str, WheelFile]:
    """``checking``: fresh PyPI answer, in-use gate; returns the target wheels."""
    from clio_agent.providers.components.status import group_targets  # noqa: PLC0415

    indexes = {name: env.lookup.releases(name, refresh=True) for name in spec.distributions}
    installed = _installed_in(env, spec)
    job.from_versions = installed
    missing = [name for name, version in installed.items() if not version]
    if missing:
        raise UpdateFailed("component_not_installed", f"not installed: {', '.join(missing)}")
    targets = group_targets(spec, indexes)
    if not all(targets.values()):
        raise UpdateFailed(
            "component_no_installable_release", "no release is installable on this computer"
        )
    job.to_versions = targets
    if env.release_runtimes is not None:
        env.release_runtimes(spec.provider_kind)
    if os.name == "nt":
        result = env.run([env.python, "-c", _IN_USE_SCRIPT, json.dumps(list(spec.distributions))])
        in_use = json.loads(result.stdout or "[]") if result.returncode == 0 else []
        if in_use:
            raise UpdateFailed(
                "component_in_use",
                "a provider binary is running and cannot be replaced: " + ", ".join(in_use),
            )
    return {name: indexes[name].installable[targets[name]] for name in spec.distributions}


def _installed_in(env: UpdateEnvironment, spec: ProviderComponents) -> dict[str, str]:
    frozen = _freeze(env)
    return {name: frozen.get(name, "") for name in spec.distributions}


def _freeze(env: UpdateEnvironment) -> dict[str, str]:
    result = env.run([env.python, "-c", _FREEZE_SCRIPT])
    if result.returncode != 0:
        raise UpdateFailed("environment_unreadable", _tail(result))
    return dict(json.loads(result.stdout))


def _previous_wheels(
    env: UpdateEnvironment, spec: ProviderComponents, previous: dict[str, str]
) -> dict[str, WheelFile]:
    wheels: dict[str, WheelFile] = {}
    for name, version in previous.items():
        wheel = env.lookup.releases(name).installable.get(version)
        if wheel is None:
            raise UpdateFailed(
                "rollback_unavailable",
                f"{name} {version} has no downloadable wheel to roll back to; refusing to update",
            )
        wheels[name] = wheel
    return wheels


def _install(
    env: UpdateEnvironment, spec: ProviderComponents, wheels: list[Path], workdir: Path
) -> None:
    frozen = _freeze(env)
    constraints = workdir / "constraints.txt"
    keep = sorted(
        f"{name}=={version}" for name, version in frozen.items() if name not in spec.distributions
    )
    constraints.write_text("\n".join(keep) + "\n", encoding="utf-8")
    result = env.run(install_command(env.python, constraints, spec.distributions, wheels))
    if result.returncode != 0:
        raise UpdateFailed("install_failed", _tail(result))
    recompiled = env.run([env.python, "-c", _RECOMPILE_SCRIPT, json.dumps(list(spec.modules))])
    if recompiled.returncode != 0:
        raise UpdateFailed("bytecode_compile_failed", _tail(recompiled))


def _verify_versions(
    env: UpdateEnvironment, spec: ProviderComponents, expected: dict[str, str]
) -> None:
    result = env.run(
        [env.python, "-c", _VERSION_SCRIPT, json.dumps(expected), json.dumps(list(spec.modules))]
    )
    if result.returncode != 0:
        raise UpdateFailed(
            "verify_failed",
            _tail(result) if result.returncode != 3 else f"installed {result.stdout.strip()}",
        )


def _perform(job: UpdateJob, env: UpdateEnvironment) -> None:
    spec = env.spec or components_for(job.provider_kind)
    if spec is None:
        raise UpdateFailed(
            "provider_has_no_components", f"{job.provider_kind} has no updatable components"
        )
    target_wheels = _check(job, env, spec)
    if all(job.from_versions[n] == job.to_versions[n] for n in spec.distributions):
        return  # already current: done, nothing changed
    job.stage = "downloading"
    with tempfile.TemporaryDirectory(prefix="clio-component-update-") as tmp:
        workdir = Path(tmp)
        (workdir / "target").mkdir()
        (workdir / "previous").mkdir()
        rollback_wheels = _previous_wheels(env, spec, job.from_versions)
        targets = [env.download(target_wheels[n], workdir / "target") for n in spec.distributions]
        previous = [
            env.download(rollback_wheels[n], workdir / "previous") for n in spec.distributions
        ]
        job.stage = "installing"
        try:
            _install(env, spec, targets, workdir)
            job.changed = True
            job.stage = "verifying"
            _verify_versions(env, spec, job.to_versions)
            if env.verify_provider is not None:
                outcome = env.verify_provider(env.python, spec.provider_kind)
                if not outcome.ok:
                    raise UpdateFailed(outcome.code or "provider_check_failed", outcome.detail)
        except UpdateFailed as failure:
            _rollback(job, env, spec, previous, workdir, failure)
            raise
    job.restart_required = _loaded_here(env, spec)


def _rollback(
    job: UpdateJob,
    env: UpdateEnvironment,
    spec: ProviderComponents,
    previous: list[Path],
    workdir: Path,
    failure: UpdateFailed,
) -> None:
    """Reinstall the downloaded previous wheels; record whether that worked."""
    try:
        _install(env, spec, previous, workdir)
        _verify_versions(env, spec, job.from_versions)
    except UpdateFailed as exc:
        logger.error("component rollback failed reason=rollback_failed detail=%s", exc)
        failure.code = "rollback_failed"
        failure.args = (f"{failure.args[0]} (rollback failed: {exc})",)
        job.changed = True
        return
    job.rolled_back = True
    job.changed = False


def _loaded_here(env: UpdateEnvironment, spec: ProviderComponents) -> bool:
    """Whether THIS process imported the group's old code (it keeps it until restart)."""
    try:
        same = os.path.samefile(env.python, sys.executable)
    except OSError:
        same = False
    return same and any(module in sys.modules for module in spec.modules)


#: The process-wide updater the routes use.
UPDATER = ComponentUpdater()


__all__ = [
    "ComponentUpdater",
    "UPDATER",
    "UpdateEnvironment",
    "UpdateFailed",
    "UpdateInProgressError",
    "UpdateJob",
    "VerifyOutcome",
    "download_wheel",
    "install_command",
    "run_command",
    "verify_provider_in_child",
]
