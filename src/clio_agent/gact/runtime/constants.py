"""Server-wide wire + limit constants for the GACT server (#714 decomposition).

These are the cross-concern scalar constants that the extracted route modules and
the assembly shell (:mod:`clio_agent.gact.app`) both reference. Folding them into a
leaf -- it imports only the config resolver -- lets the route modules read them
without importing back into ``app.py`` (which would violate the no-cycle
invariant). ``app.py`` re-exports them so existing
``from clio_agent.gact.app import <name>`` callers stay green.

* :data:`CONTRACT_VERSION` -- the GACT contract version advertised by
  ``GET /v1/capabilities``.
* :data:`GACT_BACKEND_VERSION` -- this backend build's version, surfaced by
  ``GET /v1/health`` / ``GET /v1/capabilities`` and the per-session SSE
  ``server.connected`` event.
* :data:`_CTX_MAX_BYTES` -- the per-attached-file inline cap context injection and
  the ``/v1/memory/stats`` retained-token estimate both respect.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import clio_agent
from clio_agent import conf

CONTRACT_VERSION = "0.2"


def _installed_clio_agent_version() -> str:
    """Return the installed package version exposed by the backend API."""

    try:
        return metadata.version("clio-agent")
    except metadata.PackageNotFoundError:
        return str(getattr(clio_agent, "__version__", "0.0.0"))


def _git_head_sha() -> str | None:
    """Return the short HEAD SHA when running from a git checkout, else ``None``.

    Resolves ``.git/HEAD`` (and its ref, incl. ``packed-refs``) by reading files
    rather than shelling out. Any missing-repo/parse error yields ``None`` so the
    caller falls back to the plain semver.
    """

    try:
        git_dir = Path(clio_agent.__file__).resolve().parent.parent.parent / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_file = git_dir / ref
            if ref_file.exists():
                sha = ref_file.read_text(encoding="utf-8").strip()
            else:  # ref lives in packed-refs
                packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
                sha = next(
                    line.split(" ", 1)[0]
                    for line in packed.splitlines()
                    if line.rstrip().endswith(ref)
                )
        else:  # detached HEAD holds the SHA directly
            sha = head
        return sha[:8] or None
    except (OSError, IndexError, StopIteration, ValueError):
        # Not a git checkout / unreadable or malformed HEAD refs: the documented
        # fallback is the plain semver, so returning None here is expected
        # behaviour, not a swallowed error (narrow except keeps it non-blind).
        return None


def _backend_version() -> str:
    """Return this build's version, SHA-suffixed (``0.5.17+d626a90f``) in a checkout."""

    base = _installed_clio_agent_version()
    sha = _git_head_sha()
    return f"{base}+{sha}" if sha else base


GACT_BACKEND_VERSION = _backend_version()

_CTX_MAX_BYTES = conf.resolve(
    "limits.context_inline_bytes",
    env="CLIO_CTX_MAX_BYTES",
    default=32 * 1024,  # 32 KB cap per attached file
    cast=conf.as_int,
)
