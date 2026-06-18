#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GACT_ROOT="${GACT_TUI_ROOT:-$ROOT/external/gact-tui}"
OUT="${1:-$ROOT/dist/clio-tui}"
# Absolutize OUT *before* the `cd "$GACT_ROOT/tui"` below — otherwise a relative
# output path (the release workflow passes "clio-tui-<os>-<arch>") makes
# `go build -o "$OUT"` write the binary inside the gact-tui/tui subdir instead of
# the caller's cwd, so the caller's sha256sum/upload can't find it (silent break).
case "$OUT" in
  /*) ;;
  *) OUT="$PWD/$OUT" ;;
esac

mkdir -p "$(dirname "$OUT")"

PKG="github.com/JaimeCernuda/gact-tui/tui/internal/config"
ldflags=(
  "-s"
  "-w"
  "-X" "${PKG}.DefaultBrand=clio"
  "-X" "${PKG}.builtinBrandName=CLIO"
  "-X" "${PKG}.builtinBrandTag=CLIO"
  "-X" "${PKG}.builtinBrandGlyph=C"
  "-X" "${PKG}.builtinBrandAccent=#ea7b2a"
)

(
  cd "$GACT_ROOT/tui"
  CGO_ENABLED="${CGO_ENABLED:-0}" go build -trimpath -ldflags "${ldflags[*]}" -o "$OUT" .
)

"$OUT" version >/dev/null 2>&1 || true
