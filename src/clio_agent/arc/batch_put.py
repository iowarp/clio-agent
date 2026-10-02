"""Independent ARC record puts issued together (``ARCStore.put_many``).

A working-set append writes independent records -- the content-lane chunk and the
search-companion chunk (body and text). On clio-core they are issued together on the
async put path and awaited together, so an append costs about one round trip instead
of one per blob. Every blob of a batch completes (or exhausts its bounded retry)
before the batch answers; a lost blob is a :class:`BatchPutError` naming what was
written and what was not -- never a partial silent success.
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.errors import ClioError

__all__ = ["BatchPutError", "PutRecord"]


@dataclass(frozen=True)
class PutRecord:
    """One record of a batch put (same arguments as ``ARCStore.put``)."""

    name: str
    data: bytes
    search_text: str | None = None


class BatchPutError(ClioError):
    """clio-core did not store every blob of a batch put; the others were written."""

    reason = "arc_batch_put_failed"

    def __init__(self, kind: str, written: list[str], failed: dict[str, BaseException]) -> None:
        self.written = list(written)
        self.failed = dict(failed)
        names = ", ".join(sorted(failed))
        super().__init__(
            f"clio-core did not store {len(failed)} of {len(failed) + len(written)} "
            f"{kind} blobs ({names}); written: {', '.join(self.written) or 'none'}",
            error_type=self.reason,
            details={
                "kind": kind,
                "written": self.written,
                "failed": {name: type(exc).__name__ for name, exc in self.failed.items()},
            },
        )

    @property
    def failed_names(self) -> list[str]:
        """The blob names that were not stored, sorted."""
        return sorted(self.failed)
