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
# window title + splash wordmark. Version identity still belongs to the pinned
# GACT source, so stamp the same fields as GACT's Makefile instead of falling
# back to the historical development version embedded by a plain `go build`.
version_pkg="github.com/JaimeCernuda/gact-tui/tui/internal/version"
if git -C "$GACT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  gact_revision="$(git -C "$GACT_ROOT" rev-parse HEAD)"
  gact_build_time="$(git -C "$GACT_ROOT" show -s --format=%cI HEAD)"
  if [[ -n "$(git -C "$GACT_ROOT" status --porcelain --untracked-files=no)" ]]; then
    gact_dirty=true
  else
    gact_dirty=false
  fi
else
  # Docker source contexts intentionally omit .git. The release workflow
  # passes the exact submodule identity as build arguments so the compiled TUI
  # keeps authoritative provenance without depending on repository metadata
  # inside the image build context.
  gact_revision="${GACT_TUI_REVISION:-}"
  gact_build_time="${GACT_TUI_BUILD_TIME:-}"
  gact_dirty="${GACT_TUI_DIRTY:-false}"
  if [[ -z "$gact_revision" || -z "$gact_build_time" ]]; then
    echo "GACT source is not a git checkout; set GACT_TUI_REVISION and GACT_TUI_BUILD_TIME" >&2
    exit 1
  fi
fi
gact_package_version="$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$GACT_ROOT/package.json" | head -n 1)"
if [[ -z "$gact_package_version" ]]; then
  echo "could not read the GACT release version from $GACT_ROOT/package.json" >&2
  exit 1
fi
gact_release="v${gact_package_version}"
ldflags=(
  "-s"
  "-w"
  "-X" "${version_pkg}.Release=${gact_release}"
  "-X" "${version_pkg}.BuildRevision=${gact_revision}"
  "-X" "${version_pkg}.BuildTime=${gact_build_time}"
  "-X" "${version_pkg}.BuildDirty=${gact_dirty}"
)

(
  cd "$GACT_ROOT/tui"
  CGO_ENABLED="${CGO_ENABLED:-0}" GOWORK=off go build -trimpath -ldflags "${ldflags[*]}" -o "$OUT" .
)

# Validate every target as a Go executable without attempting to run a
# cross-compiled binary on the Linux release runner. Native builds still run
# the product-level version command as the stronger smoke check.
go version -m "$OUT" >/dev/null
target_goos="${GOOS:-$(go env GOOS)}"
target_goarch="${GOARCH:-$(go env GOARCH)}"
if [[ "$target_goos" == "$(go env GOHOSTOS)" && "$target_goarch" == "$(go env GOHOSTARCH)" ]]; then
  "$OUT" version
fi
