"""#771 truth pass: persisted version stamps reflect the installed build.

Note (#948 S4b): the core agent's ``ClioAgent._store_conversation`` (and its
``_clio_agent_version`` helper) were deleted with the Tier-1 planner; the gact
route's ``_installed_clio_agent_version`` is now the sole stamping helper.

The gact compact route (``routes/sessions.py`` ``POST /v1/sessions/{sid}/compact``)
used to hard-code ``clio_agent_version="0.2.0"`` into ARC conversation metadata via
its ``store_compact_conversation`` mirror; that whole mirror -- and the route-level
version stamp it carried -- was DELETED by #1339 (``gact/compact_memory.py``: sole
writer of the ARC ``conversations`` record, readers unreachable). Compaction no
longer stamps a version anywhere, so the route-level regression lock this file used
to carry (``test_gact_compact_route_stamps_installed_version``) has nothing left to
guard and is gone with it; ``_installed_clio_agent_version`` itself is still real
(``_backend_version`` below still calls it) and stays covered.
"""

from __future__ import annotations

from importlib import metadata

INSTALLED_VERSION = metadata.version("clio-agent")


def test_version_helpers_report_installed_distribution() -> None:
    """The gact stamping helper resolves to the installed distribution version.

    #948 S4b: the core ``clio_agent.agent._clio_agent_version`` helper was deleted
    with the planner; ``_installed_clio_agent_version`` is the surviving stamp.
    """

    from clio_agent.gact.runtime.constants import _installed_clio_agent_version

    assert _installed_clio_agent_version() == INSTALLED_VERSION
    assert INSTALLED_VERSION != "0.2.0"


def test_backend_version_appends_git_sha_in_checkout() -> None:
    """``GACT_BACKEND_VERSION`` carries the HEAD SHA (``semver+sha``) in a checkout.

    Outside a git repo the helper degrades to the plain semver; either shape is
    accepted, but the SHA suffix, when present, must be a short hex of the base.
    """

    from clio_agent.gact.runtime.constants import _backend_version, _git_head_sha

    version = _backend_version()
    sha = _git_head_sha()
    if sha is None:
        assert version == INSTALLED_VERSION
    else:
        assert version == f"{INSTALLED_VERSION}+{sha}"
        assert 0 < len(sha) <= 8
        assert all(c in "0123456789abcdef" for c in sha)
