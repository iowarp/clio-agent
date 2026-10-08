"""Wire models for an infrastructure operation's structured progress.

An operation (``POST /v1/infrastructure/services/{id}/actions``) carries,
besides its one-line ``progress`` text:

* ``steps``: the ordered plan (inspect, each install step, waiting for the
  server, finishing), each with a ``state`` and its own timing;
* ``current_step``: the index of the running step (None before and after);
* per step ``progress``: ``determinate`` with ``fraction`` only where the
  amount is actually measured (bytes downloaded against a registry total,
  layers, build percent, packages against a lock), otherwise indeterminate
  with whatever counter is known -- never an invented percentage;
* ``reused``: what a reuse preflight verified and skipped;
* ``log_cursor``: the id of the newest event on the operation's live stream
  (``GET /v1/infrastructure/operations/{id}/events``); pass it as
  ``Last-Event-ID`` (or ``after``) to continue from this record.

Every field has a default, so a client that predates them still decodes it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StepState = Literal["pending", "running", "succeeded", "reused", "skipped", "failed", "cancelled"]
ProgressUnit = Literal["bytes", "layers", "packages", "percent", "items"]


class StepProgress(BaseModel):
    """How far one step is, as far as it can honestly be measured."""

    determinate: bool = False
    unit: ProgressUnit | None = None
    current: float | None = None
    total: float | None = None
    #: 0..1, set only when ``determinate``.
    fraction: float | None = None
    detail: str = ""


class OperationStep(BaseModel):
    """One ordered step of an operation."""

    id: str
    label: str
    state: StepState = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    progress: StepProgress | None = None
    message: str = ""


class ReuseReport(BaseModel):
    """A verified, skipped install step (see :mod:`~.reuse`)."""

    kind: str
    thing: str
    identity: str
    path: str = ""
    size_bytes: int | None = None
    saved_seconds: float | None = None
    message: str = ""
    step: int | None = None


class OperationProgressFields(BaseModel):
    """The structured-progress fields an :class:`InfrastructureOperation` carries."""

    steps: list[OperationStep] = Field(default_factory=list)
    current_step: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    reused: list[ReuseReport] = Field(default_factory=list)
    from_scratch: bool = False
    log_cursor: int = 0


__all__ = [
    "OperationProgressFields",
    "OperationStep",
    "ProgressUnit",
    "ReuseReport",
    "StepProgress",
    "StepState",
]
