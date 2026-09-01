"""The CMF write mode is decided by the conf declaration, and nothing else.

The owner bar for deployment shape (a): ``server_url`` alone gives a working
write path on every client OS, every unsupported combination refuses with a
typed reason, and nothing depends on a manual step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.artifacts.provenance.cmf_mode import build_cmf_provider, resolve_cmf_mode
from clio_agent.gact.artifacts.provenance.cmf_reasons import CMFRefusal
from clio_agent.gact.artifacts.provenance.cmf_server_mode import CMFServerModeProvider
from tests._config_layer import set_config


def test_server_url_alone_selects_server_mode() -> None:
    assert resolve_cmf_mode(server_url="http://cmf.test", python="") == "server"


def test_python_alone_selects_the_local_worker() -> None:
    assert resolve_cmf_mode(server_url="", python="/usr/bin/python3") == "worker"


def test_both_keeps_the_worker_writing_and_publishing() -> None:
    """The worker owns the durable local store, which is what makes a push
    retryable across a server outage."""
    assert (
        resolve_cmf_mode(server_url="http://cmf.test", python="/usr/bin/python3")
        == "worker+publish"
    )


def test_neither_is_the_typed_no_write_target_refusal() -> None:
    with pytest.raises(CMFRefusal) as excinfo:
        resolve_cmf_mode(server_url="", python="")
    assert excinfo.value.reason == "cmf_no_write_target"
    assert "configure_server_url" in excinfo.value.payload["recovery_actions"]


def test_whitespace_is_not_a_declaration() -> None:
    with pytest.raises(CMFRefusal) as excinfo:
        resolve_cmf_mode(server_url="   ", python="  ")
    assert excinfo.value.reason == "cmf_no_write_target"


def test_worker_url_is_reserved_vocabulary_and_refuses_today() -> None:
    with pytest.raises(CMFRefusal) as excinfo:
        resolve_cmf_mode(server_url="http://cmf.test", python="", worker_url="http://worker.test")
    assert excinfo.value.reason == "cmf_worker_url_unsupported"


def test_declaring_only_server_url_builds_a_queryable_server_provider(tmp_path: Path) -> None:
    """End to end through conf: one key, a working write AND read path."""
    set_config("provenance.artifacts.cmf.server_url", "http://cmf.test")
    set_config("provenance.artifacts.cmf.pipeline_name", "clio-declared")

    provider = build_cmf_provider(tmp_path)

    assert isinstance(provider, CMFServerModeProvider)
    assert provider.name == "cmf"
    assert provider.queryable is True
    assert provider.durable is True
    assert provider.config.pipeline_name == "clio-declared"
    # Server mode still offers CLIO's custody options; neither needs cmflib.
    assert provider.store.name == "cmf"
    probe = provider.probe()
    assert probe["mode"] == "server"
    assert probe["server_url"] == "http://cmf.test"


def test_server_mode_carries_the_publish_timeout_into_the_reader(tmp_path: Path) -> None:
    set_config("provenance.artifacts.cmf.server_url", "http://cmf.test")
    set_config("provenance.artifacts.cmf.publish_timeout_s", 7.5)

    provider = build_cmf_provider(tmp_path)

    assert provider.config.publish_timeout_s == 7.5


def test_no_declaration_refuses_at_build_time_not_at_first_write(tmp_path: Path) -> None:
    """Boot-time honesty: a CMF provider with nowhere to write says so at once."""
    set_config("provenance.artifacts.cmf.server_url", "")
    set_config("provenance.artifacts.cmf.python", "")

    with pytest.raises(CMFRefusal) as excinfo:
        build_cmf_provider(tmp_path)
    assert excinfo.value.reason == "cmf_no_write_target"
