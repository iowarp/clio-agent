"""User-updatable provider components: which CLI runs, update checks, in-place updates.

* :mod:`.client_binary` -- installed vs bundled CLI selection for the Codex and
  Claude Code SDK transports.
* :mod:`.registry` -- the ONLY user-updatable distributions.
* :mod:`.pypi` / :mod:`.status` -- latest installable release and
  ``update_available`` per provider.
* :mod:`.updater` / :mod:`.verify` -- the staged in-place update with rollback.

Submodules are imported directly; this package imports nothing on its own so a
provider-catalog read never pays for the updater.
"""
