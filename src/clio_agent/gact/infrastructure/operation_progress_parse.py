"""Read honest, measurable progress out of a step's live output.

A :class:`StepMeter` is fed every output line of one step and keeps the best
measure the output actually supports, in this order:

1. bytes: a ``CLIO_PROGRESS`` line from CLIO's own step scripts (an Apptainer
   pull's registry total and layer-cache growth) -- determinate only with a
   known, non-zero total;
2. layers: Docker's per-layer status lines (it announces every layer first, so
   the total is known), or ``Copying blob`` lines against a known layer total;
3. a build's ``[ 45%]`` (CMake/Make) or ``[12/80]`` (Ninja) counter;
4. packages: pip ``Collecting`` lines against the lock's requirement count, or
   uv's ``Resolved``/``Installed`` summaries.

Anything else is indeterminate: a counter and/or the latest phase line, never
a fraction.
"""

from __future__ import annotations

import json
import re

from clio_agent.gact.infrastructure.operation_models import StepProgress
from clio_agent.gact.infrastructure.reuse import PROGRESS_MARKER

_DOCKER_LAYER = re.compile(
    r"^([0-9a-f]{12}): (Pulling fs layer|Waiting|Downloading|Verifying Checksum|"
    r"Download complete|Extracting|Pull complete|Already exists)"
)
_COPYING_BLOB = re.compile(r"Copying blob (?:sha256:)?([0-9a-f]{8,})(.*)$")
_PERCENT = re.compile(r"^\[\s*(\d{1,3})%\]")
_NINJA = re.compile(r"^\[(\d+)/(\d+)\]")
_UV_RESOLVED = re.compile(r"\bResolved (\d+) packages?\b")
_UV_INSTALLED = re.compile(r"\bInstalled (\d+) packages?\b")
_PHASE = re.compile(r"^(?:INFO|WARNING):\s+(.+)$")


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


class StepMeter:
    """Accumulate one step's output into its best honest progress measure."""

    def __init__(self) -> None:
        self.bytes_current: float | None = None
        self.bytes_total: float | None = None
        self.layers_total: float | None = None
        self.layers_seen: set[str] = set()
        self.layers_done: set[str] = set()
        self.docker_layers = False
        self.percent: float | None = None
        self.items: tuple[float, float] | None = None
        self.packages_total: float | None = None
        self.packages_current: float | None = None
        self.detail = ""

    def feed(self, line: str) -> bool:
        """Take one output line; True when the measure may have changed."""

        text = line.strip()
        if not text:
            return False
        if text.startswith(PROGRESS_MARKER):
            return self._structured(text[len(PROGRESS_MARKER) :])
        if match := _DOCKER_LAYER.match(text):
            self.docker_layers = True
            self.layers_seen.add(match.group(1))
            if match.group(2) in {"Pull complete", "Already exists"}:
                self.layers_done.add(match.group(1))
            return True
        if match := _COPYING_BLOB.search(text):
            self.layers_seen.add(match.group(1))
            if any(word in match.group(2) for word in ("done", "skipped", "exists")):
                self.layers_done.add(match.group(1))
            return True
        if match := _PERCENT.match(text):
            self.percent = min(100.0, float(match.group(1)))
            return True
        if match := _NINJA.match(text):
            done, total = float(match.group(1)), float(match.group(2))
            self.items = (min(done, total), total) if total > 0 else None
            return True
        if text.startswith("Collecting "):
            self.packages_current = (self.packages_current or 0) + 1
            return True
        if match := _UV_RESOLVED.search(text):
            self.packages_total = float(match.group(1))
            self.detail = text
            return True
        if match := _UV_INSTALLED.search(text):
            self.packages_current = float(match.group(1))
            self.packages_total = self.packages_total or self.packages_current
            return True
        if match := _PHASE.match(text):
            self.detail = match.group(1)[:200]
            return True
        return False

    def _structured(self, payload: str) -> bool:
        try:
            data = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        unit = data.get("unit")
        current, total = _number(data.get("current")), _number(data.get("total"))
        if unit == "bytes":
            if total is not None:
                self.bytes_total = total
            if current is not None:
                self.bytes_current = current
        elif unit == "packages":
            if total is not None:
                self.packages_total = total
            if current is not None:
                self.packages_current = current
        if (layers := _number(data.get("layers"))) is not None:
            self.layers_total = layers
        cached = _number(data.get("cached_layers"))
        if cached and self.layers_total:
            self.detail = f"{cached:.0f} of {self.layers_total:.0f} layers already cached"
        if isinstance(data.get("detail"), str):
            self.detail = data["detail"][:200]
        return True

    def measure(self) -> StepProgress | None:
        """The step's progress now, or None when its output says nothing measurable."""

        if self.bytes_current is not None or self.bytes_total:
            if self.bytes_total:
                current = min(self.bytes_current or 0.0, self.bytes_total)
                return _determinate("bytes", current, self.bytes_total, self.detail)
            return StepProgress(unit="bytes", current=self.bytes_current, detail=self.detail)
        if self.layers_seen:
            total = len(self.layers_seen) if self.docker_layers else self.layers_total
            if total:
                return _determinate("layers", len(self.layers_done), float(total), self.detail)
            return StepProgress(
                unit="layers", current=float(len(self.layers_done)), detail=self.detail
            )
        if self.percent is not None:
            return _determinate("percent", self.percent, 100.0, self.detail)
        if self.items is not None:
            return _determinate("items", self.items[0], self.items[1], self.detail)
        if self.packages_current is not None or self.packages_total:
            if self.packages_total and self.packages_current is not None:
                current = min(self.packages_current, self.packages_total)
                return _determinate("packages", current, self.packages_total, self.detail)
            return StepProgress(
                unit="packages",
                current=self.packages_current,
                total=None,
                detail=self.detail
                or (f"{self.packages_total:.0f} packages" if self.packages_total else ""),
            )
        if self.detail:
            return StepProgress(detail=self.detail)
        return None


def _determinate(unit: str, current: float, total: float, detail: str) -> StepProgress:
    return StepProgress(
        determinate=True,
        unit=unit,  # type: ignore[arg-type]
        current=current,
        total=total,
        fraction=round(max(0.0, min(1.0, current / total)), 4),
        detail=detail,
    )


__all__ = ["StepMeter"]
