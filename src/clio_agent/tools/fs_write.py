"""Shared filesystem write operations for CLIO edit workflows."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import validate_non_empty_string, validate_write_path


def write_text_with_policy(filepath: str, new_content: str) -> dict[str, Any]:
    """Write text after enforcing CLIO's write file policy.

    This is the single implementation behind the MCP ``fs_apply_edit_write``
    tool and the user-approved GACT ``/diffs/apply`` path. Callers may add
    their own permission prompts or audit records before invoking it.

    The bytes on disk are the author's bytes, verbatim. ``newline=""`` disables
    Python's text-mode newline translation, which under its default
    ``newline=None`` rewrites every ``\\n`` to ``os.linesep`` — i.e. to ``\\r\\n``
    on Windows. That translation is silent corruption for any consumer that is
    not Windows: observed live (p5run2) when a compute expert authored a POSIX
    shell script here, the relay staged the file's bytes to a Linux cluster, and
    the job died on ``hostname\\r: not found`` while the following ``echo`` line
    still printed, because a trailing CR is invisible. A caller that genuinely
    wants CRLF puts CRLF in ``new_content`` and now gets exactly that.

    The return dict carries ``sha256`` — the content hash of the bytes actually
    on disk after the write (mechanism ``harness``: the write is the evidence).
    The hash is taken of the on-disk bytes (not the pre-encode string) so it
    equals what a consumer re-hashing the file computes, which is the whole
    point of provenance (detection). The gact-side caller mints the
    ``artifact.created`` record from it (#966 S1 seam b).
    """

    validate_non_empty_string(filepath, field="filepath")
    safe = validate_write_path(filepath, field="filepath")
    path = Path(safe)
    body = new_content if isinstance(new_content, str) else str(new_content)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    on_disk = path.read_bytes()
    return {
        "path": str(path),
        "size_bytes": len(on_disk),
        "sha256": hashlib.sha256(on_disk).hexdigest(),
        "ok": True,
    }
