"""The doctor's rows for optional pieces: each reports and none is required.

Gathered here so the runtime probe lists them in one entry
(:meth:`clio_agent.runtime.status.RuntimeProbe.collect`).
"""

from __future__ import annotations

from typing import Any


def optional_rows() -> list[Any]:
    """The compaction prompt, the browser, ripgrep and Clio Coder rows, in that order."""
    from clio_agent.compaction_prompt import probe_compaction_prompt  # noqa: PLC0415
    from clio_agent.gact.browser.install import probe_browser  # noqa: PLC0415
    from clio_agent.runtime.clio_coder.doctor import probe_clio_coder  # noqa: PLC0415
    from clio_agent.runtime.ripgrep import probe_ripgrep  # noqa: PLC0415

    return [probe_compaction_prompt(), probe_browser(), probe_ripgrep(), probe_clio_coder()]
