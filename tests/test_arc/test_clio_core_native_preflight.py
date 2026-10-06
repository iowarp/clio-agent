"""Unit tests for the native-startup preflight (the child that may exit instead of us)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from clio_agent.arc import clio_core_attach
from clio_agent.arc import clio_core_native_preflight as preflight
from clio_agent.arc.clio_core_attach import ClioCoreAttachError
from clio_agent.arc.init_degradation import (
    CLIO_CORE_CLIENT_ATTACH_TIMEOUT,
    CLIO_CORE_NATIVE_CLIENT_EXIT,
    classify_init_failure,
)

#: A stand-in for the real extension module: it has a native origin, like the one the
#: attach calls in production (``clio_cte_core_ext.cp312-win_amd64.pyd``).
_NATIVE = SimpleNamespace(
    __name__="clio_cte_core_ext", __spec__=SimpleNamespace(origin="clio_cte_core_ext.pyd")
)


@pytest.fixture(autouse=True)
def _fresh_removed_record():
    preflight.reset_removed_embedded_runtime_env()
    yield
    preflight.reset_removed_embedded_runtime_env()


def _runner(code: int | None, output: str):
    seen: dict[str, object] = {}

    def run(argv: list[str], env: Mapping[str, str], window: float) -> tuple[int | None, str]:
        seen.update(argv=argv, env=dict(env), window=window)
        return code, output

    return run, seen


def test_a_child_that_reached_the_marker_returned() -> None:
    run, seen = _runner(0, "noise\nCLIO_NATIVE_PREFLIGHT_RETURNED\n")
    result = preflight.preflight_native_client(
        _NATIVE, config_path="d.yaml", no_progress_s=9, runner=run
    )
    assert result.returned is True
    assert seen["env"]["CLIO_SERVER_CONF"] == "d.yaml"
    assert seen["env"]["CLIO_WAIT_SERVER"] == "0"  # the child never contacts the daemon
    assert seen["window"] == 9


@pytest.mark.parametrize(("code", "output"), [(1, "FATAL LoadFromFile bad conversion"), (0, "")])
def test_a_child_that_died_or_never_reached_the_marker_did_not_return(
    code: int, output: str
) -> None:
    run, _ = _runner(code, output)
    result = preflight.preflight_native_client(
        _NATIVE, config_path="d.yaml", no_progress_s=9, runner=run
    )
    assert result.returned is False
    assert result.exit_code == code
    assert result.output == output


def test_the_preflight_bound_never_drops_below_the_default_stall() -> None:
    assert preflight.preflight_window_s(3.0) == 30.0
    assert preflight.preflight_window_s(45.0) == 45.0


def test_an_embedded_runtime_variable_is_removed_and_recorded() -> None:
    environ = {"CLIO_WITH_RUNTIME": "1", "OTHER": "x"}
    assert preflight.remove_embedded_runtime_env(environ) == {"CLIO_WITH_RUNTIME": "1"}
    assert environ == {"OTHER": "x"}
    assert preflight.removed_embedded_runtime_env() == {"CLIO_WITH_RUNTIME": "1"}
    assert preflight.remove_embedded_runtime_env(environ) == {}


def _cte(calls: list[str]) -> SimpleNamespace:
    def init(mode: object, flag: bool) -> bool:
        calls.append("clio_init")
        return True

    return SimpleNamespace(clio_init=init, RuntimeMode=SimpleNamespace(kClient="k"))


@pytest.mark.parametrize(
    ("code", "reason", "phrase"),
    [
        (1, CLIO_CORE_NATIVE_CLIENT_EXIT, "ended its process during startup (exit code 1)"),
        (None, CLIO_CORE_CLIENT_ATTACH_TIMEOUT, "did not finish in its child process"),
    ],
)
def test_a_preflight_that_did_not_return_degrades_typed_and_never_attaches(
    monkeypatch: pytest.MonkeyPatch, code: int | None, reason: str, phrase: str
) -> None:
    monkeypatch.setattr(
        preflight,
        "preflight_native_client",
        lambda *_a, **_kw: preflight.NativePreflightResult(
            returned=False, exit_code=code, output="FATAL x"
        ),
    )
    calls: list[str] = []
    deregistered: list[bool] = []
    with pytest.raises(ClioCoreAttachError) as info:
        clio_core_attach.attach_native_client(
            _cte(calls), config_path="c.yaml", port=1, on_failure=lambda: deregistered.append(True)
        )
    assert classify_init_failure(info.value) == reason
    assert info.value.stage == "native_preflight"
    assert phrase in str(info.value)
    assert "FATAL x" in str(info.value)
    assert calls == []  # the in-process native client never ran
    assert deregistered == [True]


def test_the_attach_removes_an_inherited_embedded_runtime_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_WITH_RUNTIME", "1")
    seen: list[str | None] = []

    def _preflight(*_a: object, **_kw: object) -> preflight.NativePreflightResult:
        seen.append(os.environ.get("CLIO_WITH_RUNTIME"))
        return preflight.NativePreflightResult(returned=True, exit_code=0, output="")

    monkeypatch.setattr(preflight, "preflight_native_client", _preflight)
    calls: list[str] = []
    clio_core_attach.attach_native_client(
        _cte(calls), config_path="c.yaml", port=1, on_failure=lambda: None
    )
    assert seen == [None]  # removed before the child (and the real attach) ran
    assert calls == ["clio_init"]
    assert preflight.removed_embedded_runtime_env() == {"CLIO_WITH_RUNTIME": "1"}


def test_a_module_without_native_code_needs_no_child() -> None:
    """The preflight follows the attach's own seam: the module it is handed. One with no
    native extension behind it (an in-process fake, a pure-Python shim) has no code that
    can exit the process, so no child runs."""

    def _never(*_a: object, **_kw: object) -> tuple[int | None, str]:
        raise AssertionError("no native library: nothing to preflight")

    fake = SimpleNamespace(clio_init=lambda *_a: True, RuntimeMode=SimpleNamespace(kClient="k"))
    result = preflight.preflight_native_client(
        fake, config_path="c.yaml", no_progress_s=9, runner=_never
    )
    assert (result.returned, result.skipped_reason) == (True, "no_native_library")


def test_the_child_imports_the_module_the_attach_calls() -> None:
    run, seen = _runner(0, "CLIO_NATIVE_PREFLIGHT_RETURNED")
    preflight.preflight_native_client(_NATIVE, config_path="c.yaml", no_progress_s=9, runner=run)
    source = seen["argv"][-1]
    assert "import_module('clio_cte_core_ext')" in source


def test_the_real_extension_has_a_native_origin() -> None:
    cte = pytest.importorskip("clio_cte_core_ext")
    assert preflight.native_origin(cte)


def _child(source: str) -> list[str]:
    return [__import__("sys").executable, "-c", source]


def test_a_slow_but_working_child_is_waited_for_past_the_window() -> None:
    """A slow interpreter start on a slow machine: the child works (CPU) for longer than
    the no-progress window and still finishes; never cut off at a fixed bound.

    **Sabotage:** ``subprocess.run(..., timeout=no_progress_s)`` -> killed at 0.3 s.
    """
    burn = (
        "import time\n"
        "end = time.monotonic() + 1.5\n"
        "while time.monotonic() < end:\n"
        "    pass\n"
        "print('CLIO_NATIVE_PREFLIGHT_RETURNED', flush=True)\n"
    )
    code, output = preflight._run_child(_child(burn), dict(os.environ), 0.3)
    assert code == 0
    assert "CLIO_NATIVE_PREFLIGHT_RETURNED" in output


def test_a_wedged_child_is_ended_at_the_window_and_reported() -> None:
    """A child doing nothing (a wedged library load) ends after one no-progress window."""
    import time  # noqa: PLC0415

    started = time.monotonic()
    code, output = preflight._run_child(
        _child("import time; print('loading', flush=True); time.sleep(60)"),
        dict(os.environ),
        0.5,
    )
    assert code is None  # did not return
    assert "loading" in output
    assert time.monotonic() - started < 20.0
