#!/usr/bin/env bash
# Install CLIO Desktop on macOS without a Python, uv, or Node prerequisite.
# CLIO_VERSION accepts a release tag or PyPI beta spelling; default: latest stable.
# CLIO_DESKTOP_DIR defaults to ~/Applications. --download-only DIR verifies only.
set -euo pipefail

die() { printf 'CLIO Desktop: %s\n' "$*" >&2; exit 1; }
download_only=""
case "${1:-}" in
  --download-only) [ "$#" -eq 2 ] || die 'usage: desktop.sh --download-only DIR'; download_only="$2" ;;
  --help) echo 'CLIO_VERSION=v0.9.5-beta.2 bash desktop.sh [--download-only DIR]'; exit 0 ;;
  '') ;;
  *) die "unknown option: $1" ;;
esac
[ "$(uname -s)" = Darwin ] || die 'Use desktop.ps1 on Windows or the bundled .deb/.rpm on Linux.'
case "$(uname -m)" in
  arm64|aarch64) arch=aarch64; variant=bundled ;;
  x86_64) arch=x64; variant=lite ;;
  *) die 'unsupported processor' ;;
esac
# uname can describe the Rosetta shell rather than the physical machine.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
  arch=aarch64; variant=bundled
fi
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 14 ] || die 'This Desktop release requires macOS 14 or newer.'
repo=https://github.com/iowarp/clio-agent
tag="${CLIO_VERSION:-}"
if [ -z "$tag" ]; then
  resolved="$(curl --proto '=https' --tlsv1.2 -fsSL -o /dev/null -w '%{url_effective}' "$repo/releases/latest")"
  tag="${resolved##*/}"
fi
version="${tag#v}"
if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)b([0-9]+)$ ]]; then
  version="${BASH_REMATCH[1]}-beta.${BASH_REMATCH[2]}"
fi
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+|-beta\.[0-9]+)?$ ]] || die "invalid release version: $tag"
tag="v$version"
suffix=""; [ "$variant" != bundled ] || suffix=-bundled
target=aarch64-apple-darwin
[ "$arch" != x64 ] || target=x86_64-apple-darwin
asset="CLIO.Desktop_${version}_${arch}${suffix}.dmg"
checksums="SHA256SUMS.${target}.${variant}.txt"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/clio-desktop.XXXXXXXX")"
mounted=0
cleanup() {
  if [ "$mounted" = 1 ]; then hdiutil detach "$scratch/mount" -quiet || true; fi
  rm -rf "$scratch"
}
trap cleanup EXIT
printf 'Downloading %s (%s)\n' "$tag" "$variant"
curl --proto '=https' --tlsv1.2 -fSL --retry 3 "$repo/releases/download/$tag/$asset" -o "$scratch/$asset"
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$repo/releases/download/$tag/$checksums" -o "$scratch/checksums"
expected="$(awk -v name="$asset" '$2 == name {print $1}' "$scratch/checksums")"
[[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || die 'release checksum missing or ambiguous'
actual="$(shasum -a 256 "$scratch/$asset" | awk '{print $1}')"
[ "$actual" = "$expected" ] || die 'download checksum mismatch; nothing installed'
if [ -n "$download_only" ]; then
  mkdir -p "$download_only"
  cp "$scratch/$asset" "$download_only/$asset"
  printf 'Verified download: %s/%s\n' "$download_only" "$asset"
  exit 0
fi
if pgrep -x clio-desktop >/dev/null; then die 'Quit CLIO Desktop before installing.'; fi
hdiutil verify "$scratch/$asset"
mkdir "$scratch/mount"
hdiutil attach "$scratch/$asset" -readonly -nobrowse -mountpoint "$scratch/mount" -quiet
mounted=1
app="$scratch/mount/CLIO Desktop.app"
[ -d "$app" ] || die 'disk image does not contain CLIO Desktop.app'
codesign --verify --deep --strict "$app" || die 'App signature is invalid or absent; nothing installed. Use a release with verified macOS bundles.'
destination="${CLIO_DESKTOP_DIR:-$HOME/Applications}"
case "$destination" in /*) ;; *) die 'CLIO_DESKTOP_DIR must be absolute' ;; esac
mkdir -p "$destination"
staging="$(mktemp -d "$destination/.clio-install.XXXXXXXX")"
ditto "$app" "$staging/CLIO Desktop.app"
codesign --verify --deep --strict "$staging/CLIO Desktop.app"
backup=""
if [ -e "$destination/CLIO Desktop.app" ]; then
  backup="$destination/CLIO Desktop.previous.$(date +%Y%m%d%H%M%S).$$.app"
  mv "$destination/CLIO Desktop.app" "$backup"
fi
if ! mv "$staging/CLIO Desktop.app" "$destination/CLIO Desktop.app"; then
  [ -z "$backup" ] || mv "$backup" "$destination/CLIO Desktop.app"
  die 'could not install app; previous app restored'
fi
rmdir "$staging"
printf 'Installed: %s/CLIO Desktop.app\n' "$destination"
[ -z "$backup" ] || printf 'Previous app retained: %s\n' "$backup"
if [ "$variant" = lite ]; then echo 'Intel package: connect to a separately installed CLIO service.'; fi
echo 'Open CLIO Desktop from Applications. macOS may require Privacy & Security > Open Anyway.'
echo 'This release is ad-hoc signed, not Apple-notarized. No security settings were changed.'
