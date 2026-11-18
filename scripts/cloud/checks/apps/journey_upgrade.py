"""Make a plugin store as a CLIO from before ``follows`` left it (first-run journey JR-U1).

Run with the stack's environment (``XDG_CONFIG_HOME`` pointing at an empty folder) before
starting the stack:

    uv run --no-sync python scripts/cloud/checks/apps/journey_upgrade.py

It installs the default marketplace the way an older reader did: the reader without
``follows`` and reader version 2. A plugin that follows a connector is then recorded as
invalid ("unknown ai.iowarp.clio field(s): follows") and left out, as on an existing
install. The script prints the store's state for the following plugins; start the stack
afterwards and the upgraded reader re-reads the snapshot.
"""

from __future__ import annotations

import argparse
import json
from unittest import mock

from clio_agent.plugins import manifest, store
from clio_agent.plugins.service import DEFAULT_MARKETPLACE, PluginService, plugin_store_root


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    fields = tuple(f for f in manifest._EXTENSION_FIELDS if f != "follows")
    with (
        mock.patch.object(manifest, "_EXTENSION_FIELDS", fields),
        mock.patch.object(store, "READER_VERSION", 2),
    ):
        service = PluginService(store.PluginStore(plugin_store_root()))
        service.ensure_default_marketplace()
    state = service.store.read_state()
    entry = state["marketplaces"][DEFAULT_MARKETPLACE]
    report = {
        "store": str(plugin_store_root()),
        "reader": entry.get("reader"),
        "follows_diagnostics": [
            row for row in entry.get("diagnostics", []) if "follows" in row.get("message", "")
        ],
        "following_plugins": sorted(
            key for key, row in state["plugins"].items() if row.get("follows")
        ),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
