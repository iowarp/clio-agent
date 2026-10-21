#!/bin/bash
# Claude Code cloud environment setup script for clio-agent.
#
# Paste this file's contents into the environment's "Setup script" field at
# claude.ai/code. It provisions the VM only (toolchains), runs before Claude Code
# launches, and is cached as a filesystem snapshot when it finishes in about five
# minutes. Project dependencies are installed by scripts/cloud/session_start.sh,
# which the repository's SessionStart hook runs on every session.
#
# It must exit 0: a non-zero exit stops the session from starting.

set -u

# uv is preinstalled on the cloud image; install it only if a future image drops it.
command -v uv >/dev/null 2>&1 || pip install --break-system-packages uv || true

# The interpreter clio-agent targets. uv downloads it from PyPI-hosted mirrors.
uv python install 3.13 >/dev/null 2>&1 || true

# gact-tui pins pnpm through "packageManager"; corepack provides that exact version.
corepack enable >/dev/null 2>&1 || true
corepack prepare pnpm@9.15.9 --activate >/dev/null 2>&1 || true

# Pre-fill the uv and pnpm caches so each session's SessionStart install takes
# seconds instead of the ~8 minutes a cold install takes. The snapshot is saved
# only when this script finishes in about five minutes, so the warm-up is
# time-boxed; whatever it downloads in that window is kept.
WARM=/tmp/clio-cache-warm
rm -rf "$WARM"
if git clone -q --depth 1 --branch "${CLIO_WARM_BRANCH:-codex/agent-facing-async-tasks}" \
    https://github.com/iowarp/clio-agent.git "$WARM"; then
  (cd "$WARM" && timeout 200 env UV_PROJECT_ENVIRONMENT="$WARM/.venv" uv sync --extra dev -q) || true
  (cd "$WARM" && git submodule update --init --depth 1 external/gact-tui -q \
    && cd external/gact-tui && timeout 60 pnpm install --frozen-lockfile --silent) || true
fi
rm -rf "$WARM"

# Headless Chromium for UI checks. The image ships one under /opt/pw-browsers;
# report it rather than downloading, since the Playwright CDN is not on the
# default allowlist.
if ls /opt/pw-browsers/chromium-*/chrome-linux/chrome >/dev/null 2>&1; then
  echo "chromium: $(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome | tail -1)"
else
  echo "chromium: not found under /opt/pw-browsers; UI checks will fail until one is installed"
fi

exit 0
