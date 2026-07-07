"""#771 truth pass: persisted version stamps reflect the installed build.

Both the core agent (``agent.py``) and the gact compaction route
(``routes/sessions.py``) used to hard-code ``clio_agent_version="0.2.0"`` into
ARC conversation metadata while the package was on 0.5.x. They now stamp the
real installed version via a shared helper; these tests pin that.
"""

from __future__ import annotations

from importlib import metadata


def _expected_version() -> str:
    try:
        return metadata.version("clio-agent")
    except metadata.PackageNotFoundError:
        import clio_agent  # noqa: PLC0415

        return str(getattr(clio_agent, "__version__", "0.0.0"))


def test_gact_constants_version_matches_installed() -> None:
    from clio_agent.gact.runtime.constants import _installed_clio_agent_version

    assert _installed_clio_agent_version() == _expected_version()


def test_agent_version_helper_matches_installed() -> None:
    from clio_agent.agent import _clio_agent_version

    assert _clio_agent_version() == _expected_version()


def test_version_stamp_is_not_the_stale_literal() -> None:
    from clio_agent.agent import _clio_agent_version

    # The whole point of the fix: never the frozen "0.2.0" again.
    assert _clio_agent_version() != "0.2.0"
