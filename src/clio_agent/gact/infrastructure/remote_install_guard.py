"""Protect user content even when bootstrapping an older released installer."""

INSTALL_GUARD = r"""
backup=""
install_ok=0
restore_install() {
  local status=$? entry name
  trap - EXIT
  if [ -n "$backup" ]; then
    if [ "$install_ok" = 1 ]; then
      shopt -s dotglob nullglob
      for entry in "$backup/previous/"*; do
        name="${entry##*/}"
        case "$name" in .venv|web) continue ;; esac
        if [ -e "$root/clio-agent/$name" ] || [ -L "$root/clio-agent/$name" ]; then
          printf 'Preserved pre-upgrade content in %s (destination occupied).\n' "$entry"
          status=75
        else
          mv -- "$entry" "$root/clio-agent/$name" || status=75
        fi
      done
      # Only executable payloads are disposable. Unknown content stays backed up.
      rm -rf -- "$backup/previous/.venv" "$backup/previous/web"
      rmdir -- "$backup/previous" "$backup" 2>/dev/null || true
    else
      if [ -e "$root/clio-agent" ]; then
        mv -- "$root/clio-agent" "$backup/failed-install" || exit 75
      fi
      mv -- "$backup/previous" "$root/clio-agent" || exit 75
      printf 'Upgrade failed; restored the previous installation and data.\n'
    fi
  fi
  exit "$status"
}
if [ -d "$root/clio-agent" ]; then
  mkdir -p "$root/user/state/install-backups" || exit 75
  backup="$(mktemp -d "$root/user/state/install-backups/upgrade-XXXXXXXX")" || exit 75
  mv -- "$root/clio-agent" "$backup/previous" || exit 75
fi
trap restore_install EXIT
"""
