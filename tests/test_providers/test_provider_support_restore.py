"""Restoring recorded provider support after the backend environment was replaced.

Unit coverage of the recording seams, the planner, the restorer's
typed states and the provider status a missing support reports. The
end-to-end replacement with a real ``uv`` environment is in
``test_provider_support_runtime_replacement.py``.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.providers import dependencies, support_record, support_restore
from clio_agent.providers.components import updater


@pytest.fixture(autouse=True)
def _clean_restorer() -> None:
    support_restore.RESTORER.reset()
    support_record.reset_record_failures()
    yield
    support_restore.RESTORER.reset()


# --- recording at the install seam ------------------------------------------


@pytest.mark.real_dependency_installer
def test_installing_claude_code_support_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(
        dependencies, "claude_code_requirement", lambda: "claude-agent-sdk==0.2.159"
    )
    availability = iter((False, False, False, True))
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
    monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_k: subprocess.CompletedProcess(command, 0, "", "")
    )

    assert dependencies.ensure_claude_code_support(python_executable=str(python)) is True
    assert support_record.read_recorded_support().entries == {"claude_code": {}}


@pytest.mark.real_dependency_installer
def test_installing_alcf_support_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    availability = iter((False, False, True))
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: next(availability))
    monkeypatch.setattr(dependencies, "_uv_executable", lambda _python: "uv")
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_k: subprocess.CompletedProcess(command, 0, "", "")
    )

    assert dependencies.ensure_argonne_support(python_executable="python") is True
    assert support_record.read_recorded_support().entries == {"argonne": {}}


def test_support_the_runtime_already_carries_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The desktop bundles ALCF sign-in: that is not the person's install."""
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: True)

    assert dependencies.ensure_argonne_support() is False
    assert dependencies.ensure_claude_code_support() is False
    assert support_record.read_recorded_support().entries == {}


def test_a_failed_install_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "_module_available", lambda _name: False)
    monkeypatch.setattr(
        dependencies, "claude_code_requirement", lambda: "claude-agent-sdk==0.2.159"
    )

    with pytest.raises(dependencies.ProviderDependencyInstallError):
        dependencies.ensure_claude_code_support()  # the suite refuses real installs
    assert support_record.read_recorded_support().entries == {}


def test_a_finished_component_update_records_its_versions_as_the_floor() -> None:
    recorded: list[tuple[str, dict[str, str]]] = []

    def _perform(job: updater.UpdateJob, _env: updater.UpdateEnvironment) -> None:
        job.to_versions = {"openai-codex-cli-bin": "0.158.0"}
        job.changed = True

    env = updater.UpdateEnvironment(record_support=lambda k, v: recorded.append((k, v)))
    runner = updater.ComponentUpdater()
    original = updater._perform
    updater._perform = _perform  # type: ignore[assignment]
    try:
        job = runner.run("codex", env)
    finally:
        updater._perform = original  # type: ignore[assignment]

    assert job.stage == "done"
    assert recorded == [("codex", {"openai-codex-cli-bin": "0.158.0"})]


def test_an_unchanged_or_failed_component_update_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[str] = []
    env = updater.UpdateEnvironment(record_support=lambda k, _v: recorded.append(k))

    monkeypatch.setattr(updater, "_perform", lambda job, _env: None)  # already current
    assert updater.ComponentUpdater().run("codex", env).stage == "done"

    def _fail(_job: updater.UpdateJob, _env: updater.UpdateEnvironment) -> None:
        raise updater.UpdateFailed("install_failed", "resolver said no")

    monkeypatch.setattr(updater, "_perform", _fail)
    assert updater.ComponentUpdater().run("codex", env).stage == "failed"
    assert recorded == []


# --- planning ---------------------------------------------------------------


def test_a_recorded_support_whose_module_is_gone_is_planned_for_install() -> None:
    steps = support_restore.plan_restores(
        {"claude_code": {}, "argonne": {}},
        installed=lambda kind: kind == "argonne",
        version_of=lambda _name: "",
    )
    assert steps == [
        support_restore.RestoreStep("claude_code", "install", support_restore.SUPPORT_MISSING)
    ]


def test_a_component_older_than_its_recorded_floor_is_planned_for_update() -> None:
    versions = {"openai-codex-cli-bin": "0.157.1"}
    steps = support_restore.plan_restores(
        {"codex": {"openai-codex-cli-bin": "0.158.0"}},
        installed=lambda _kind: True,
        version_of=versions.__getitem__,
    )
    assert steps == [
        support_restore.RestoreStep(
            "codex",
            "update",
            support_restore.COMPONENT_BELOW_RECORD,
            {"openai-codex-cli-bin": "0.158.0"},
        )
    ]


def test_nothing_is_planned_when_the_environment_already_satisfies_the_record() -> None:
    steps = support_restore.plan_restores(
        {
            "claude_code": {"claude-agent-sdk": "0.2.150"},
            # A distribution no longer in the registry (the removed Codex SDK
            # package, a typo) is not a component and plans nothing.
            "codex": {"openai-codex-cli-bin": "0.157.1", "openai-codex": "9.9.9", "bogus": "9"},
            "not_a_provider": {},
        },
        installed=lambda _kind: True,
        version_of=lambda name: {
            "claude-agent-sdk": "0.2.159",
            "openai-codex-cli-bin": "0.160.0",
        }.get(name, "garbage-version"),
    )
    assert steps == []


# --- the restorer ------------------------------------------------------------


def _steps(*kinds: str) -> list[support_restore.RestoreStep]:
    return [
        support_restore.RestoreStep(kind, "install", support_restore.SUPPORT_MISSING)
        for kind in kinds
    ]


def test_a_restore_is_visible_as_restoring_before_it_runs_and_restored_after() -> None:
    restorer = support_restore.RESTORER
    jobs = restorer.claim(_steps("claude_code"))
    assert support_restore.missing_support_status("claude_code") == (
        "support_restoring",
        "Restoring Claude Code support after the update…",
    )
    installed: list[str] = []
    settled: list[str] = []

    restorer.execute(
        jobs,
        install=installed.append,
        update=lambda _k: None,
        on_settled=lambda job: settled.append(f"{job.provider_kind}:{job.state}"),
    )

    assert installed == ["claude_code"]
    assert settled == ["claude_code:restored"]
    assert restorer.job("claude_code").state == "restored"  # type: ignore[union-attr]
    assert not restorer.running


def test_a_failed_restore_says_so_plainly_and_install_retries_it() -> None:
    restorer = support_restore.RESTORER
    jobs = restorer.claim(_steps("claude_code", "argonne"))

    def _install(kind: str) -> None:
        if kind == "claude_code":
            raise dependencies.ProviderDependencyInstallError("no route to pypi.org")

    restorer.execute(jobs, install=_install, update=lambda _k: None)

    failed = restorer.job("claude_code")
    assert failed is not None and failed.state == "failed"
    assert failed.error_code == support_restore.PROVIDER_SUPPORT_RESTORE_FAILED
    assert failed.to_wire()["error"] == {
        "code": "provider_support_restore_failed",
        "message": "no route to pypi.org",
    }
    status, message = support_restore.missing_support_status("claude_code")
    assert status == "install_required"  # the Install button is the retry
    assert "could not restore Claude Code support after the update" in message
    assert "no route to pypi.org" in message
    # One failure does not stop the next provider's restore.
    assert restorer.job("argonne").state == "restored"  # type: ignore[union-attr]


def test_a_component_restore_fails_with_the_update_jobs_own_code() -> None:
    restorer = support_restore.RESTORER
    step = support_restore.RestoreStep(
        "codex",
        "update",
        support_restore.COMPONENT_BELOW_RECORD,
        {"openai-codex-cli-bin": "0.158.0"},
    )
    jobs = restorer.claim([step])

    restorer.execute(
        jobs,
        install=lambda _k: None,
        update=lambda _k: SimpleNamespace(
            stage="failed", error_code="rolled_back", error="verify failed"
        ),
    )

    job = restorer.job("codex")
    assert job is not None and (job.state, job.error_code) == ("failed", "rolled_back")


def test_a_second_restore_pass_is_refused_while_one_runs() -> None:
    restorer = support_restore.RESTORER
    jobs = restorer.claim(_steps("claude_code"))
    gate = threading.Event()
    worker = threading.Thread(
        target=restorer.execute,
        args=(jobs,),
        kwargs={"install": lambda _k: gate.wait(5), "update": lambda _k: None},
    )
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            restorer.claim(_steps("argonne"))
    finally:
        gate.set()
        worker.join(5)
    assert restorer.claim(_steps("argonne"))  # free again once the pass finished


def test_without_a_restore_a_missing_support_is_plain_install_required() -> None:
    assert support_restore.missing_support_status("claude_code") == (
        "install_required",
        "Claude Code support is not installed on the connected agent.",
    )
    assert support_restore.missing_support_status("argonne")[0] == "install_required"


def test_the_alcf_row_reports_its_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers import argonne_auth

    monkeypatch.delenv("CLIO_ARGONNE_TOKEN", raising=False)
    monkeypatch.delenv("ALCF_INFERENCE_TOKEN", raising=False)
    monkeypatch.setattr(argonne_auth, "sdk_available", lambda: False)
    support_restore.RESTORER.claim(_steps("argonne"))

    assert argonne_auth.readiness() == (
        "support_restoring",
        "Restoring ALCF sign-in support after the update…",
        False,
    )
