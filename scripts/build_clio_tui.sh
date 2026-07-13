#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GACT_ROOT="${GACT_TUI_ROOT:-$ROOT/external/gact-tui}"
OUT="${1:-$ROOT/dist/clio-tui}"
# Normalize Windows backslashes to forward slashes. The Windows installer
# (install.ps1) runs this under git-bash/msys and passes a native path like
# `C:\Users\me\AppData\Local\clio\gact.exe`; bash treats `\` as a literal
# char, so without this `dirname` returns "." and the absoluteness check below
# misfires.
OUT="${OUT//\\//}"
# Absolutize OUT *before* the `cd "$GACT_ROOT/tui"` below — otherwise a relative
# output path (the release workflow passes "clio-tui-<os>-<arch>") makes
# `go build -o "$OUT"` write the binary inside the gact-tui/tui subdir instead of
# the caller's cwd, so the caller's sha256sum/upload can't find it (silent break).
# A Windows drive-letter path (C:/...) is already absolute; only a POSIX `/...`
# was recognized before, so native Windows paths were wrongly treated as
# relative and mangled into `$PWD/C:/...` (the binary then landed under the
# clio-agent checkout instead of the install prefix — `clio` could not find it).
case "$OUT" in
  /* | [A-Za-z]:/*) ;;
  *) OUT="$PWD/$OUT" ;;
esac

mkdir -p "$(dirname "$OUT")"

# gact-tui is brand-neutral and the Go TUI no longer bakes a brand at build time:
# the old `-X internal/config.DefaultBrand=…/builtinBrand*` ldflags were removed
# upstream (the brand symbols no longer exist; `-X` against a missing symbol is a
# silent no-op). The TUI is now white-labeled purely at runtime — the `clio`
# launcher exports `GACT_BRAND_NAME=CLIO` (see install/clio), which drives the
# window title + splash wordmark. So this build stays a plain, stripped build.
ldflags=("-s" "-w")

(
  cd "$GACT_ROOT/tui"
  CGO_ENABLED="${CGO_ENABLED:-0}" go build -trimpath -ldflags "${ldflags[*]}" -o "$OUT" .
)

"$OUT" version >/dev/null 2>&1 || true
