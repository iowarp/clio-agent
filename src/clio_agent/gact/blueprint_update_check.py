"""Read-only marketplace update check for agent-blueprint sources.

The desktop versions panel wants to tell a user "a newer version of this
marketplace is available" without cutting a CLIO release and without
mutating anything on disk. Every existing blueprint-source action --
``POST .../refresh``, ``POST .../update`` -- installs; this module answers
"is there something to install" by comparing each registered source's
recorded ``commit`` (owned by :mod:`clio_agent.gact.agent_blueprint_sources`)
against the source's CURRENT remote head, via ``git ls-remote`` /
``git rev-parse`` probes that touch no working tree.

This is the "surface reality" half of the cleanup-program ground rule
(system-cleanup-2026-07.md #775): every outcome -- success or one of the
typed failure reasons in :data:`UpdateCheckReason` -- is reported, never
silently collapsed to "no update" or dropped. There is no decision-making
here (no routing, no auto-install); the caller (a desktop panel, or the
GACT routes in ``routes/blueprint_updates.py``) decides what to do with the
comparison.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import psutil

UpdateCheckReason = Literal[
    "up_to_date",
    "update_available",
    "source_not_found",
    "installed_commit_unknown",
    "git_unavailable",
    "ls_remote_failed",
    "ref_not_found",
    "timeout",
    "path_source_not_git",
]


@dataclass(frozen=True)
class SourceUpdateStatus:
    """One source row's result from a single read-only update probe.

    Attributes:
        source_id: The registered source's ``src_*`` id.
        source: The source's git URL or local path, verbatim.
        ref: The registered branch/tag ref (empty means the remote's HEAD).
        installed_commit: The commit recorded at the last refresh/install
            (``commit``, falling back to ``pinned_commit``), empty when unknown.
        remote_commit: The commit the source's ref currently resolves to,
            empty when the probe could not resolve one.
        update_available: ``True``/``False`` when comparable, ``None`` when
            :attr:`reason` reports why no comparison could be made.
        reason: Typed outcome -- see :data:`UpdateCheckReason`.
        detail: Free-text diagnostic (e.g. a git stderr tail); empty on a
            clean comparison.
    """

    source_id: str
    source: str
    ref: str
    installed_commit: str
    remote_commit: str
    update_available: bool | None
    reason: UpdateCheckReason
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        """Return the JSON-serializable wire representation."""

        return asdict(self)


def blueprint_source_ls_remote_timeout_s() -> float:
    """Seconds a single ``git ls-remote``/``rev-parse`` update probe may take.

    Config: ``gact.blueprint_source.ls_remote_timeout_s`` /
    ``CLIO_BLUEPRINT_SOURCE_LS_REMOTE_TIMEOUT_S`` (default 10.0). Lower it to
    keep the desktop versions panel snappy against a stalled remote, raise it
    for a slow link.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "gact.blueprint_source.ls_remote_timeout_s",
        env="CLIO_BLUEPRINT_SOURCE_LS_REMOTE_TIMEOUT_S",
        default=10.0,
        cast=conf.as_float,
    )


def _popen_kwargs() -> dict[str, Any]:
    """Windows: keep the console window hidden during a probe subprocess."""

    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _stderr_tail(stderr: str, *, max_chars: int = 500) -> str:
    """Return the trailing slice of a subprocess's stderr, or empty string."""

    text = (stderr or "").strip()
    return text[-max_chars:] if text else ""


def _git_env() -> dict[str, str]:
    """Environment for a probe subprocess: never prompt, never hang on auth.

    ``GIT_SSH_COMMAND`` (or the older ``GIT_SSH``) is AUGMENTED, not overwritten:
    a user with a custom SSH wrapper (a specific key/identity, a bastion) set one
    for a reason, and clobbering it here would silently break their transport
    while only fixing an unrelated prompt hang. ``-o BatchMode=yes`` is safe to
    append to any ``ssh``-shaped command -- it is an ordinary repeatable option.

    ``GIT_SSH_COMMAND`` is already a shell-parsed command line (git splits it
    itself), so an existing value is only ever augmented verbatim. ``GIT_SSH``
    is different: it names a bare executable PATH, never shell syntax, so it
    is quoted before being folded into ``GIT_SSH_COMMAND`` -- otherwise a path
    containing spaces (e.g. ``C:\\Program Files\\OpenSSH\\ssh.exe``) would be
    split on whitespace and git would try to exec ``C:\\Program`` with
    ``Files\\OpenSSH\\ssh.exe`` as a bogus argument.
    """

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    existing_ssh_command = (env.get("GIT_SSH_COMMAND") or "").strip()
    if existing_ssh_command:
        base = existing_ssh_command
    else:
        existing_ssh_path = (env.get("GIT_SSH") or "").strip()
        base = shlex.quote(existing_ssh_path) if existing_ssh_path else "ssh"
    env["GIT_SSH_COMMAND"] = f"{base} -o BatchMode=yes"
    return env


def _kill_process_tree(pid: int) -> None:
    """Kill a probe subprocess and every descendant, never just the immediate child.

    On Windows the resolved ``git.exe`` spawns its OWN transport child
    (``git-remote-https.exe`` / ``ssh.exe``) which inherits the parent's stdio
    pipes; killing only the ``git.exe`` PID leaves that child running and
    holding the pipes open, so ``Popen.communicate()`` blocks well past the
    caller's timeout waiting for EOF that never comes (proven live: a 2s probe
    timeout took ~25s to actually return). Enumerated via psutil (an existing
    core dependency; same idiom as ``tools/relay_install_jobs.py``'s own
    process-tree teardown) -- children killed before the parent, best-effort,
    never raises into the caller.
    """

    children: list[psutil.Process] = []
    parent: psutil.Process | None = None
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    for child in children:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            child.kill()
    if parent is not None:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            parent.kill()
    with suppress(Exception):  # noqa: BLE001 - best-effort reap, never blocks the caller
        psutil.wait_procs([*children, *([parent] if parent is not None else [])], timeout=5)


def remote_head_commit(
    source: str, ref: str, *, timeout_s: float
) -> tuple[str, UpdateCheckReason, str]:
    """Resolve the commit a source's ref (or HEAD) currently points at.

    A local-path source (``Path(source).exists()``) is probed with
    ``git -C <path> rev-parse HEAD``; a remote source with
    ``git ls-remote --exit-code <source> refs/heads/<ref> refs/tags/<ref>
    refs/tags/<ref>^{}`` (``HEAD`` when ``ref`` is empty). Never clones, never
    writes.

    The third pattern matters for an ANNOTATED tag: ``refs/tags/<ref>`` alone
    resolves to the tag OBJECT's sha, not the commit it points at, so a
    tag-pinned source would report a permanent false ``update_available``
    (installed commit sha vs. tag-object sha never equal) even when fully up
    to date. ``refs/tags/<ref>^{}`` is git's own "peel to commit" ref form and
    resolves it explicitly; a lightweight tag has no such peeled line, so the
    plain ``refs/tags/<ref>`` entry (already the commit sha there) is still the
    fallback.

    Args:
        source: Git URL or local filesystem path.
        ref: Branch/tag name, or empty for the remote's default HEAD.
        timeout_s: Subprocess timeout in seconds.

    Returns:
        ``(commit, reason, detail)``. ``commit`` is empty on any failure;
        ``reason`` is always a typed :data:`UpdateCheckReason` (on success it
        is the placeholder ``"up_to_date"`` -- callers that need the actual
        up-to-date/update-available verdict get it from
        :func:`check_source_update`, which compares this commit against the
        installed one).
    """

    source = source.strip()
    ref = ref.strip()
    local_path = Path(source).expanduser()
    is_local = local_path.exists()
    if is_local:
        command = ["git", "-C", str(local_path), "rev-parse", "HEAD"]
    else:
        refs = (
            [f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"] if ref else ["HEAD"]
        )
        command = ["git", "ls-remote", "--exit-code", source, *refs]

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_git_env(),
            **_popen_kwargs(),
        )
    except FileNotFoundError:
        return "", "git_unavailable", "git executable not found"

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # `subprocess.run(..., timeout=)` on Windows only signals the immediate
        # `git.exe` PID; a still-running transport grandchild (git-remote-https.exe
        # / ssh.exe) keeps the inherited stdout/stderr pipes open, so the plain
        # `run()` call blocks in its own internal `communicate()` for the FULL
        # process lifetime of that grandchild rather than returning at
        # `timeout_s` (proven live: a 2s timeout took ~25s to actually raise).
        # Killing the whole tree before reaping is what makes the timeout real.
        _kill_process_tree(process.pid)
        with suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=5)
        return "", "timeout", f"git probe timed out after {timeout_s}s"

    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    if is_local:
        if completed.returncode != 0:
            return (
                "",
                "path_source_not_git",
                _stderr_tail(completed.stderr) or "not a git repository",
            )
        commit = completed.stdout.strip()
        if not commit:
            return "", "path_source_not_git", "git rev-parse returned no output"
        return commit, "up_to_date", ""

    if completed.returncode == 2:
        return "", "ref_not_found", f"no ref matching {ref or 'HEAD'} at {source}"
    if completed.returncode != 0:
        return (
            "",
            "ls_remote_failed",
            _stderr_tail(completed.stderr) or f"git ls-remote exited {completed.returncode}",
        )

    by_ref: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            by_ref[parts[1]] = parts[0]
    commit = ""
    if ref:
        # Prefer a branch, then an annotated tag's PEELED commit (`^{}`), then a
        # lightweight tag's own sha -- see the annotated-tag note in the docstring.
        commit = (
            by_ref.get(f"refs/heads/{ref}")
            or by_ref.get(f"refs/tags/{ref}^{{}}")
            or by_ref.get(f"refs/tags/{ref}")
            or ""
        )
    else:
        commit = by_ref.get("HEAD", "")
    commit = commit or next(iter(by_ref.values()), "")
    if not commit:
        return "", "ls_remote_failed", "git ls-remote returned no output"
    return commit, "up_to_date", ""


def _commits_match(installed: str, remote: str) -> bool:
    """Whether ``installed`` and ``remote`` name the same commit, abbreviated-sha aware.

    A pinned/installed commit is occasionally recorded abbreviated (a human-edited
    config, or an older recorder) rather than the full 40-char sha ``git
    ls-remote``/``rev-parse`` always returns; a strict ``==`` would then report a
    permanent false ``update_available``. A 7-39 char lowercase-hex ``installed``
    value is treated as a sha PREFIX of ``remote``; anything else (full sha, or not
    hex at all) still requires exact equality. ``installed`` is lowercased before
    any comparison -- git shas are case-insensitive but always rendered lowercase
    by ``git ls-remote``/``rev-parse``, so a human-edited config recording one in
    upper/mixed case must not report a permanent false ``update_available``.
    """

    installed = installed.lower()
    if installed == remote:
        return True
    if 7 <= len(installed) < 40 and all(c in "0123456789abcdef" for c in installed):
        return remote.startswith(installed)
    return False


def check_source_update(row: Mapping[str, Any], *, timeout_s: float) -> SourceUpdateStatus:
    """Compare one source row's installed commit against its current remote head.

    Args:
        row: A blueprint-source row as loaded by
            :func:`clio_agent.gact.agent_blueprint_sources.load_agent_blueprint_sources`
            (``id``, ``source``, ``ref``, ``commit``/``pinned_commit``).
        timeout_s: Subprocess timeout in seconds, forwarded to
            :func:`remote_head_commit`.

    Returns:
        A :class:`SourceUpdateStatus` with a typed reason -- never raises on
        an unreachable remote, a missing ``git``, or an unresolvable ref.
    """

    source_id = str(row.get("id") or "")
    source = str(row.get("source") or "").strip()
    ref = str(row.get("ref") or "").strip()
    installed_commit = str(row.get("commit") or row.get("pinned_commit") or "").strip()

    if not source:
        return SourceUpdateStatus(
            source_id=source_id,
            source=source,
            ref=ref,
            installed_commit=installed_commit,
            remote_commit="",
            update_available=None,
            reason="source_not_found",
            detail="source is empty",
        )

    remote_commit, reason, detail = remote_head_commit(source, ref, timeout_s=timeout_s)
    if not remote_commit:
        return SourceUpdateStatus(
            source_id=source_id,
            source=source,
            ref=ref,
            installed_commit=installed_commit,
            remote_commit="",
            update_available=None,
            reason=reason,
            detail=detail,
        )

    if not installed_commit:
        return SourceUpdateStatus(
            source_id=source_id,
            source=source,
            ref=ref,
            installed_commit=installed_commit,
            remote_commit=remote_commit,
            update_available=None,
            reason="installed_commit_unknown",
            detail="",
        )

    up_to_date = _commits_match(installed_commit, remote_commit)
    return SourceUpdateStatus(
        source_id=source_id,
        source=source,
        ref=ref,
        installed_commit=installed_commit,
        remote_commit=remote_commit,
        update_available=not up_to_date,
        reason="up_to_date" if up_to_date else "update_available",
        detail="",
    )


def check_all_sources(
    rows: Sequence[Mapping[str, Any]], *, timeout_s: float
) -> list[SourceUpdateStatus]:
    """Run :func:`check_source_update` over every row, sequentially.

    Sequential is fine here: the desktop's source list is a handful of rows
    (a user's registered marketplaces), not a fan-out worth parallelizing.
    """

    return [check_source_update(row, timeout_s=timeout_s) for row in rows]
