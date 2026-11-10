#!/usr/bin/env bash
# CLIO Desktop, from a URL:
#
#   curl -fsSL https://clio.iowarp.ai/desktop.sh | bash
#
# The same as `install.sh --desktop`; the website serves both files from the same commit
# (.github/workflows/pages.yml). Any arguments go through (`--dev`, `--version <v>`).
set -euo pipefail
script="$(curl -fsSL https://clio.iowarp.ai/install.sh)"
exec bash -c "$script" install.sh --desktop "$@"
