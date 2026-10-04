"""Exercise real SFTP transfer, conflict review and selected writeback on owned test data."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from clio_schemas.connected_resources import ConnectedSource, ResourceOwner

from clio_agent.gact.storage.models import SourceRecord, TransferOperation
from clio_agent.gact.storage.review import apply_review, review_changes
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.store import SourceStore
from clio_agent.gact.storage.transfers import materialize, remove_owned_tree


def qualify(profile: str, remote_root: str) -> dict[str, object]:
    """Modify and restore only the synthetic fixture beneath the owned qualification root."""
    if not remote_root.startswith("/data/clio-beta3-qualification/"):
        raise ValueError(
            "This qualification may write only beneath /data/clio-beta3-qualification/"
        )
    with tempfile.TemporaryDirectory(prefix="clio-beta3-sftp-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        store = SourceStore(root / "sources")
        adapter = SftpSource(profile, remote_root, writable=True)
        original = root / "original.txt"
        restore_revision: str | None = None
        try:
            initial = next(entry for entry in adapter.entries() if entry.path == "nested/input.txt")
            with adapter.open_read(initial) as stream:
                original.write_bytes(stream.read())
            if not original.read_bytes().startswith(b"CLIO beta-3 synthetic SFTP fixture"):
                raise ValueError("The remote file is not the expected disposable fixture")
            record = SourceRecord(
                source=ConnectedSource(
                    id="source_qualification",
                    provider="sftp",
                    label="Homelab qualification",
                    owner=ResourceOwner(clio_id=store.clio_id, host_id="local"),
                    root=remote_root,
                    mode="working_copy",
                    capabilities=adapter.capabilities,
                    workspace_id="qualification",
                ),
                principal="qualification",
                workspace_root=str(workspace),
            )
            store.put("source", record.source.id, record)
            operation = store.begin_operation(record.source.id, "materialize")
            record = materialize(store, record, adapter, workspace, operation)
            copy = Path(record.source.local_path or "")
            assert (copy / "nested/input.txt").read_bytes() == original.read_bytes()
            (copy / "nested/input.txt").write_bytes(
                original.read_bytes() + b"Reviewed beta-3 change\n"
            )
            (copy / "unselected.txt").write_text("This file must stay in the working copy")
            review = review_changes(store, record, adapter)
            assert not any(change.conflict for change in review.changes)
            assert any("Reviewed beta-3 change" in change.preview for change in review.changes)
            applied = apply_review(store, record, adapter, review.id, ["nested/input.txt"])
            current = {entry.path: entry for entry in adapter.entries()}
            restore_revision = current["nested/input.txt"].revision
            assert "unselected.txt" not in current
            with adapter.open_read(current["nested/input.txt"]) as stream:
                assert stream.read().endswith(b"Reviewed beta-3 change\n")
            restarted = SourceStore(store.root)
            assert restarted.clio_id == store.clio_id
            assert restarted.get("operation", applied, TransferOperation).state == "completed"
            retained = restarted.get("source", record.source.id, SourceRecord)
            assert retained.manifest_id == record.manifest_id
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "host_profile": profile,
                "source_root": remote_root,
                "transport": "OpenSSH profile + verified Paramiko SFTP",
                "checks": {
                    "nested_file_transfer": "passed",
                    "immutable_baseline": "passed",
                    "bounded_change_preview": "passed",
                    "selected_writeback": "passed",
                    "unselected_file_retained_locally": "passed",
                    "restart_receipts": "passed",
                },
                "live_inference": "not_requested",
                "oauth": "not_exercised",
            }
        finally:
            try:
                if restore_revision is not None:
                    adapter.apply("nested/input.txt", original, restore_revision)
            finally:
                adapter.close()
                remove_owned_tree(store.root, root)


def main() -> None:
    """Write a compact evidence receipt only after the live checks and restore succeed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-profile", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = qualify(args.ssh_profile, args.remote_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
