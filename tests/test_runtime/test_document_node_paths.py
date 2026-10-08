"""Node receives ordinary drive/UNC names even when Windows launched verbatim Python."""

from __future__ import annotations

import pytest

from clio_agent.runtime.document_stack.node_paths import node_path


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (r"\\?\C:\CLIO Desktop\npm-cli.js", r"C:\CLIO Desktop\npm-cli.js"),
        (r"\\?\D:\selected drive\node.exe", r"D:\selected drive\node.exe"),
        (r"\\?\UNC\server\share\npm-cli.js", r"\\server\share\npm-cli.js"),
        (r"C:\CLIO Desktop\npm-cli.js", r"C:\CLIO Desktop\npm-cli.js"),
        (r"\\server\share\npm-cli.js", r"\\server\share\npm-cli.js"),
        ("/opt/CLIO Desktop/node/bin/node", "/opt/CLIO Desktop/node/bin/node"),
    ],
)
def test_node_path_preserves_location_without_verbatim_prefix(original: str, expected: str) -> None:
    """Only the Windows namespace changes; spaces, drive and UNC share remain intact."""
    assert node_path(original) == expected
