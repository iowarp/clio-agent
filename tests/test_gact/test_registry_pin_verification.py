"""Registry pin verification must never silently bypass (#772, Slice 7).

When a local marketplace source is installed, the code resolves its git commit
via ``git rev-parse`` to check it against a caller-supplied ``pinned_commit``.
If that resolution fails the pin check must NOT be silently skipped:

* pinned + unresolvable commit -> raise ``ValueError`` (pin unverifiable);
* unpinned + unresolvable commit -> best-effort install + a structured
  ``reason=registry_commit_unresolvable`` warning.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from clio_agent.gact.agent_blueprints import install_agent_blueprint


def _write_min_blueprint(root: Path, blueprint_id: str = "pinned-agent") -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {blueprint_id}
version: 0.1.0
title: Pinned Agent
root_expert: root
---
Pinned marketplace agent.
""",
        encoding="utf-8",
    )
    root.joinpath("experts", "root.md").write_text(
        """---
id: root
title: Pinned Root
tier: 1
---
Coordinate work.
""",
        encoding="utf-8",
    )


def _raise_rev_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> str:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])

    monkeypatch.setattr(subprocess, "check_output", _boom)


def test_pinned_install_with_unresolvable_commit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    _write_min_blueprint(source)
    _raise_rev_parse(monkeypatch)

    with pytest.raises(ValueError, match="registry pin unverifiable"):
        install_agent_blueprint(
            source=str(source),
            scope="global",
            cwd=tmp_path / "cwd",
            home=tmp_path / "home",
            pinned_commit="deadbeef" * 5,
        )


def test_unpinned_install_with_unresolvable_commit_warns_and_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "src"
    _write_min_blueprint(source)
    _raise_rev_parse(monkeypatch)

    with caplog.at_level(logging.WARNING):
        result = install_agent_blueprint(
            source=str(source),
            scope="global",
            cwd=tmp_path / "cwd",
            home=tmp_path / "home",
            pinned_commit="",
        )

    assert result["installed"], "unpinned install should still succeed best-effort"
    assert any(
        "reason=registry_commit_unresolvable" in record.getMessage() for record in caplog.records
    ), "unresolvable commit on an unpinned install must emit a structured reason"
