"""Reuse preflight: verify what a target already holds before installing it again.

One helper for every infrastructure install (model runtimes, Flowcept, CMF,
web search, remote CLIO, Relay, model downloads). An install step asks it
whether the thing it is about to produce is already present under the SAME
verified identity -- a digest-keyed SIF in the shared image store, a local
Docker/Podman image addressed by its digest, a uv environment built from the
same project and profile, a venv built from the same lock digest and revision,
an image built from the same (revision, base, runtime), a verified model
revision, an installed CLIO of the same version -- and, when it is, skips the
step and reports it with one structured line::

    CLIO_REUSE {"kind": "sif", "thing": "...", "identity": "...", ...}

The runtime turns that line into the operation's ``reused`` entry and marks the
step ``reused`` ("Reusing <thing> (<identity>); skipped <size>/~<time>"). A
mismatch or anything missing is never reused: the step runs as before.
``install.from_scratch`` (configuration) / ``CLIO_FROM_SCRATCH=1`` (target
environment) bypasses every reuse check for one operation; it is never
persisted on the service record.

Stdlib only: this same file runs inside CLIO and is shipped beside a
supervised service as ``clio_reuse.py`` (the supervisor's worker and the stack
hooks import it there); shell steps use :data:`SHELL_FUNCTIONS`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MARKER = "CLIO_REUSE "
#: Structured progress a target step reports (see ``operation_progress_parse``).
PROGRESS_MARKER = "CLIO_PROGRESS "
#: Configuration flag that bypasses reuse for one operation (never persisted).
FROM_SCRATCH_KEY = "install.from_scratch"
#: The same request on the target side (a supervised worker's environment).
FROM_SCRATCH_ENV = "CLIO_FROM_SCRATCH"
#: The name this file is shipped under beside a supervised service.
SHIPPED_NAME = "clio_reuse.py"
#: Operation-scoped configuration keys that must never reach a service record.
TRANSIENT_KEYS = frozenset({FROM_SCRATCH_KEY})


@dataclass(frozen=True)
class Reuse:
    """One verified reuse: what was found, under which identity, and what it saved."""

    kind: str
    thing: str
    identity: str
    path: str = ""
    size_bytes: int | None = None
    saved_seconds: float | None = None

    def message(self) -> str:
        """``Reusing <thing> (<identity>); skipped <size>/~<time>``."""

        skipped = [
            part
            for part in (
                human_size(self.size_bytes) if self.size_bytes else "",
                f"~{human_seconds(self.saved_seconds)}" if self.saved_seconds else "",
            )
            if part
        ]
        tail = f"; skipped {'/'.join(skipped)}" if skipped else ""
        return f"Reusing {self.thing} ({short_identity(self.identity)}){tail}"


@dataclass(frozen=True)
class ReuseCheck:
    """A plan step whose result can prove the next steps' output is already present.

    The runtime runs the step as usual; when ``report`` returns a :class:`Reuse`
    (and the operation is not from scratch) the steps in ``skip`` are skipped
    and the reuse is reported. ``report`` gets the step's ``CommandResult``.
    """

    skip: tuple[int, ...]
    report: Callable[[Any], Reuse | None]


def short_identity(identity: str) -> str:
    """A digest shortened for people; other identities unchanged."""

    if "sha256:" in identity:
        head, _, digest = identity.partition("sha256:")
        return f"{head}sha256:{digest[:12]}"
    return identity if len(identity) <= 80 else identity[:77] + "..."


def human_size(value: int | float | None) -> str:
    """``8.1 GB`` style size (decimal units, like registries and ``du -h --si``)."""

    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def human_seconds(value: float | None) -> str:
    """``42s`` / ``7m`` / ``1h05m`` style duration."""

    if value is None:
        return ""
    seconds = int(round(value))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{round(seconds / 60)}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def line(reuse: Reuse) -> str:
    """The structured line a step prints for one verified reuse."""

    return MARKER + json.dumps(asdict(reuse), sort_keys=True)


def parse(text: str) -> list[Reuse]:
    """Every reuse line in ``text`` (other lines are ignored)."""

    found: list[Reuse] = []
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry.startswith(MARKER):
            continue
        try:
            payload = json.loads(entry[len(MARKER) :])
        except ValueError:
            continue
        if not isinstance(payload, dict) or not payload.get("kind") or not payload.get("identity"):
            continue
        size, seconds = payload.get("size_bytes"), payload.get("saved_seconds")
        found.append(
            Reuse(
                kind=str(payload["kind"]),
                thing=str(payload.get("thing") or payload["kind"]),
                identity=str(payload["identity"]),
                path=str(payload.get("path") or ""),
                size_bytes=int(size) if isinstance(size, (int, float)) and size >= 0 else None,
                saved_seconds=float(seconds)
                if isinstance(seconds, (int, float)) and seconds > 0
                else None,
            )
        )
    return found


def from_scratch(configuration: Mapping[str, str] | None) -> bool:
    """Whether this operation asked to bypass every reuse check."""

    value = (configuration or {}).get(FROM_SCRATCH_KEY, "")
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def without_transient(configuration: Mapping[str, str] | None) -> dict[str, str]:
    """``configuration`` without operation-scoped keys (what a record may keep)."""

    return {key: value for key, value in (configuration or {}).items() if key not in TRANSIENT_KEYS}


# ---- target side (stdlib; also runs as the shipped clio_reuse.py) ----------


def fresh_requested() -> bool:
    """Whether the worker was asked to install from scratch."""

    return os.environ.get(FROM_SCRATCH_ENV, "") == "1"


def read_marker(marker: Path) -> tuple[str, float | None] | None:
    """A marker's (identity, seconds the install took), or None when absent.

    Markers are JSON; a plain-text marker (CMF venvs before this helper) is its
    identity with an unknown duration.
    """

    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(payload, dict) or not isinstance(payload.get("identity"), str):
        return text, None
    seconds = payload.get("seconds")
    return payload["identity"], float(seconds) if isinstance(seconds, (int, float)) else None


def check_marker(marker: Path, identity: str, *, required: Iterable[Path] = ()) -> bool:
    """True only when ``marker`` records exactly ``identity`` and ``required`` all exist."""

    found = read_marker(marker)
    return (
        found is not None
        and found[0] == identity
        and all(path.exists() and not path.is_symlink() for path in required)
    )


def write_marker(marker: Path, identity: str, seconds: float | None = None) -> None:
    """Record that ``identity`` was installed (atomically, after it completed)."""

    temporary = marker.with_name(marker.name + ".tmp")
    temporary.write_text(
        json.dumps({"identity": identity, "seconds": seconds, "written_at": time.time()}),
        encoding="utf-8",
    )
    temporary.replace(marker)


def tree_size(path: Path) -> int:
    """Bytes under ``path`` (symlinks not followed)."""

    if path.is_file():
        return path.stat().st_size
    total = 0
    for directory, _folders, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def sif_matches(sif: Path, image: str) -> bool:
    """A SIF is reusable only with a ``.ref`` sidecar naming exactly ``image``.

    The sidecar is written after a complete pull, so a SIF without one is an
    interrupted pull (or predates the store) and is never reused.
    """

    ref = sif.with_name(sif.name + ".ref")
    try:
        recorded = ref.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return sif.is_file() and bool(recorded) and recorded[0].strip() == image


def record_sif(sif: Path, image: str, seconds: float | None = None) -> None:
    """Write the sidecars that make a completed SIF reusable (``.ref``, then ``.took``)."""

    if seconds:
        sif.with_name(sif.name + ".took").write_text(f"{seconds:.0f}\n", encoding="utf-8")
    sif.with_name(sif.name + ".ref").write_text(image + "\n", encoding="utf-8")


def sif_seconds(sif: Path) -> float | None:
    """How long the pull that produced ``sif`` took, when it was recorded."""

    try:
        return float(sif.with_name(sif.name + ".took").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def uv_environment_identity(environment: Path, profile: str) -> str:
    """A uv project environment's identity: project, lock and profile digests."""

    lock = environment / "uv.lock"
    parts = [
        hashlib.sha256((environment / "pyproject.toml").read_bytes()).hexdigest(),
        hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else "unlocked",
        profile,
    ]
    return "sha256:" + hashlib.sha256(" ".join(parts).encode()).hexdigest()


def uv_environment_reuse(environment: Path, profile: str) -> str:
    """The reuse line when ``environment/.venv`` verifiably matches, else "".

    Installing from scratch removes the venv instead (uv rebuilds it).
    """

    venv = environment / ".venv"
    if fresh_requested():
        if venv.is_dir() and not venv.is_symlink():
            shutil.rmtree(venv)
        return ""
    identity = uv_environment_identity(environment, profile)
    marker = venv / ".clio-environment"
    if not check_marker(marker, identity, required=[venv / "bin" / "python"]):
        return ""
    recorded = read_marker(marker)
    return line(
        Reuse(
            kind="uv_environment",
            thing=f"service environment {profile}".strip(),
            identity=identity,
            path=str(venv),
            size_bytes=tree_size(venv),
            saved_seconds=recorded[1] if recorded else None,
        )
    )


def record_uv_environment(environment: Path, profile: str, seconds: float) -> None:
    """Mark the environment just synced as reusable under its identity."""

    venv = environment / ".venv"
    if venv.is_dir() and not venv.is_symlink():
        identity = uv_environment_identity(environment, profile)
        write_marker(venv / ".clio-environment", identity, seconds)


def publish(reuse: Reuse, stream: Any = None) -> None:
    """Print the reuse line (and its human message) where the step's log goes."""

    output = stream or sys.stdout
    output.write(line(reuse) + "\n")
    output.flush()


#: Shell twin of :func:`publish` for steps that are shell scripts:
#: ``clio_reuse KIND THING IDENTITY PATH BYTES SECONDS`` (``-`` for unknown).
SHELL_FUNCTIONS = r"""clio_json() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
clio_num() { case "$1" in ''|-|*[!0-9.]*) printf 'null' ;; *) printf '%s' "$1" ;; esac; }
clio_reuse() {
  printf 'CLIO_REUSE {"identity": "%s", "kind": "%s", "path": "%s", "saved_seconds": %s, "size_bytes": %s, "thing": "%s"}\n' \
    "$(clio_json "$3")" "$(clio_json "$1")" "$(clio_json "$4")" "$(clio_num "$6")" \
    "$(clio_num "$5")" "$(clio_json "$2")"
}
"""


def source() -> str:
    """This file's text, shipped to targets as :data:`SHIPPED_NAME`."""

    return Path(__file__).read_text(encoding="utf-8")


__all__ = [
    "FROM_SCRATCH_ENV",
    "FROM_SCRATCH_KEY",
    "MARKER",
    "PROGRESS_MARKER",
    "SHELL_FUNCTIONS",
    "SHIPPED_NAME",
    "Reuse",
    "ReuseCheck",
    "check_marker",
    "from_scratch",
    "parse",
    "publish",
    "read_marker",
    "record_sif",
    "record_uv_environment",
    "sif_matches",
    "sif_seconds",
    "tree_size",
    "uv_environment_reuse",
    "without_transient",
    "write_marker",
]
