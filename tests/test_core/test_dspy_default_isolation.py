"""The suite restores dspy's process-default LM/adapter after every test.

Regression for an order-dependent failure: ``test_lm_bind_process_default`` drove a
real admin bind with stub factories, the bind installed the stub LM/adapter as dspy's
process default, and a later ``dspy.context(lm=DummyLM(...))`` in the same worker
(``test_module_variants_s5``) resolved the leaked ``SimpleNamespace`` adapter and
failed with "object is not callable".
"""

from __future__ import annotations

from types import SimpleNamespace

import dspy
from dspy.dsp.utils.settings import main_thread_config
from dspy.utils import DummyLM

from clio_agent.gact.runtime.ambient_lm import install_process_default_lm
from tests.conftest import _restore_dspy_process_default


def test_bind_style_install_is_undone_by_the_suite_fixture() -> None:
    before = {key: main_thread_config.get(key) for key in ("lm", "adapter")}
    guard = _restore_dspy_process_default.__wrapped__()
    next(guard)
    # What the admin bind does with stub factories: install stubs process-wide.
    assert install_process_default_lm(SimpleNamespace(model="stub"), SimpleNamespace())
    assert getattr(main_thread_config["lm"], "model", None) == "stub"
    for _ in guard:  # run the fixture's teardown
        pass
    assert {key: main_thread_config.get(key) for key in ("lm", "adapter")} == before
    # And the victim's call works again: DummyLM under dspy.context with the default adapter.
    with dspy.context(lm=DummyLM([{"answer": "ok"}])):
        assert dspy.Predict("question -> answer")(question="q").answer == "ok"
