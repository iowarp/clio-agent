#!/usr/bin/env bash
# Build the relocatable git that the bundled CLIO runtime ships (macOS / Linux).
# Windows ships MinGit instead: see build-gact-runtime.ps1.
#
# A fresh Mac has no git: /usr/bin/git is a stub that opens the "Install
# Command Line Developer Tools" dialog. CLIO needs git to install marketplace
# plugins, so the runtime carries its own (clio_agent/runtime/git_executable.py
# resolves it). The result is deliberately small:
#
#   <out>/libexec/git-core/git               the only git binary
#   <out>/libexec/git-core/git-remote-https  https/http transport (libcurl)
#   <out>/share/git-core/templates/          description + info/exclude, no hooks
#
# There is no bin/ directory and there are no symlinks or hardlinks: Tauri's
# resource walk has failed on links before, and each Mach-O stays a separate,
# signable file. git is built with RUNTIME_PREFIX, so it finds its exec-path,
# templates and system config relative to its own location
# (<out>/libexec/git-core -> <out>), whatever directory the runtime is moved to.
#
# macOS links only libraries in Apple's dyld shared cache (libSystem, libcurl,
# libz, libiconv): no Homebrew, no OpenSSL, no frameworks, so the binaries can be
# signed with the hardened runtime and notarized without entitlements, and TLS
# uses Apple's libcurl and its CA store. Linux links curl and OpenSSL statically
# and only glibc and zlib dynamically; the CA bundle is chosen at run time
# (GIT_SSL_CAINFO, set by the resolver).
#
# No Perl, Python, Tcl/Tk, gettext, docs, dashed built-ins, credential helpers,
# git-shell or upload/receive-pack binaries: CLIO only clones and fetches.
#
# Env:
#   MAKE                 GNU make (default: make)
#   CC, AR, CFLAGS, LDFLAGS
#                        toolchain overrides passed to git's Makefile
#   STRIP                strip command (default: strip; empty to skip)
#   GIT_EXTRA_MAKE_FLAGS extra make variables (word-split)
#   MACOSX_DEPLOYMENT_TARGET  minimum macOS (default: 11.0)
#   CLIO_GIT_WORK_DIR    build directory (default: a temporary directory)
#   JOBS                 parallel make jobs (default: CPU count)
#
# Usage:
#   ./build-bundled-git.sh <output-dir>

set -euo pipefail

OUT="${1:?usage: build-bundled-git.sh <output-dir>}"

GIT_VERSION="2.56.0"
GIT_SHA256="26c56c296b38c0695b26fa95f475f1d01704d2d38e73465ca30b0b2f5dc789d3"
GIT_URL="https://mirrors.edge.kernel.org/pub/software/scm/git/git-${GIT_VERSION}.tar.xz"
# Linux only: curl and OpenSSL are linked statically.
OPENSSL_VERSION="3.5.9"
OPENSSL_SHA256="603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
OPENSSL_URL="https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz"
CURL_VERSION="8.23.0"
CURL_SHA256="e3de77cfa6367083c776a26647fc9f633b538c06c3baa73d931a252ac8f72b49"
CURL_URL="https://curl.se/download/curl-${CURL_VERSION}.tar.gz"

MAKE="${MAKE:-make}"
STRIP="${STRIP-strip}"
OS="$(uname -s)"
if [ -z "${JOBS:-}" ]; then
  JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
fi

log() { echo "[build-bundled-git] $*"; }
die() { echo "build-bundled-git: $*" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

fetch() {  # url sha256 destination
  curl -fsSL --retry 3 -o "$3" "$1"
  local actual
  actual="$(sha256_of "$3")"
  [ "$actual" = "$2" ] || die "checksum mismatch for $1: expected $2, got $actual"
}

case "$OS" in
  Darwin | Linux) ;;
  *) die "unsupported OS $OS (Windows ships MinGit via build-gact-runtime.ps1)" ;;
esac

if [ -n "${CLIO_GIT_WORK_DIR:-}" ]; then
  WORK="$CLIO_GIT_WORK_DIR"
  mkdir -p "$WORK"
  CLEANUP_WORK=""
else
  WORK="$(mktemp -d)"
  CLEANUP_WORK="$WORK"
fi
trap '[ -z "$CLEANUP_WORK" ] || rm -rf "$CLEANUP_WORK"' EXIT
WORK="$(cd "$WORK" && pwd)"

rm -rf "$OUT"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

# Only used to compute paths relative to the runtime prefix; nothing is
# installed there.
PREFIX="/clio-bundled-git"
MAKE_FLAGS=(
  "prefix=$PREFIX"
  "sysconfdir=etc"
  "gitexecdir=libexec/git-core"
  "template_dir=share/git-core/templates"
  "RUNTIME_PREFIX=YesPlease"
  "NO_RUST=YesPlease"
  "NO_GETTEXT=YesPlease"
  "NO_PERL=YesPlease"
  "NO_PYTHON=YesPlease"
  "NO_TCLTK=YesPlease"
  "NO_EXPAT=YesPlease"
  "NO_INSTALL_HARDLINKS=YesPlease"
  "SKIP_DASHED_BUILT_INS=YesPlease"
  "NO_HOMEBREW=YesPlease"
  "FSMONITOR_DAEMON_BACKEND="
  "FSMONITOR_OS_SETTINGS="
  "V=1"
)
[ -z "${CC:-}" ] || MAKE_FLAGS+=("CC=$CC")
[ -z "${AR:-}" ] || MAKE_FLAGS+=("AR=$AR")
GIT_CFLAGS="${CFLAGS:--O2}"
GIT_LDFLAGS="${LDFLAGS:-}"

if [ "$OS" = "Darwin" ]; then
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
  # Apple's libcurl (Secure Transport) and its CA store; built-in SHA code
  # instead of OpenSSL/CommonCrypto. -dead_strip_dylibs drops the unused
  # CoreServices framework that git's Darwin config always adds.
  MAKE_FLAGS+=(
    "NO_OPENSSL=YesPlease"
    "NO_APPLE_COMMON_CRYPTO=YesPlease"
    "CURL_CFLAGS=${CURL_CFLAGS:--DCLIO_SYSTEM_LIBCURL}"
    "CURL_LDFLAGS=-lcurl"
  )
  GIT_LDFLAGS="$GIT_LDFLAGS -Wl,-dead_strip_dylibs"
else
  DEPS="$WORK/deps"
  mkdir -p "$DEPS"
  log "building static OpenSSL $OPENSSL_VERSION"
  fetch "$OPENSSL_URL" "$OPENSSL_SHA256" "$WORK/openssl.tar.gz"
  tar -xzf "$WORK/openssl.tar.gz" -C "$WORK"
  (
    cd "$WORK/openssl-$OPENSSL_VERSION"
    # The OpenSSL default directories are left unset (openssldir is inside the
    # build tree, which does not exist at run time); git gets its CA bundle
    # from GIT_SSL_CAINFO.
    ./config --prefix="$DEPS" --openssldir="$DEPS/ssl" --libdir=lib \
      no-shared no-tests no-docs no-apps no-engine no-module no-legacy \
      no-comp no-dso -fPIC >/dev/null
    "$MAKE" -j"$JOBS" build_libs >/dev/null
    "$MAKE" install_dev >/dev/null
  )
  log "building static curl $CURL_VERSION (http/https only)"
  fetch "$CURL_URL" "$CURL_SHA256" "$WORK/curl.tar.gz"
  tar -xzf "$WORK/curl.tar.gz" -C "$WORK"
  (
    cd "$WORK/curl-$CURL_VERSION"
    PKG_CONFIG_PATH="$DEPS/lib/pkgconfig" ./configure --prefix="$DEPS" \
      --disable-shared --enable-static --with-pic \
      --with-openssl="$DEPS" --with-zlib \
      --without-libpsl --without-brotli --without-zstd --without-nghttp2 \
      --without-nghttp3 --without-ngtcp2 --without-libidn2 --without-libssh2 \
      --without-ca-bundle --without-ca-path \
      --disable-ldap --disable-ldaps --disable-rtsp --disable-dict \
      --disable-telnet --disable-tftp --disable-pop3 --disable-imap \
      --disable-smtp --disable-gopher --disable-mqtt \
      --disable-ftp --disable-file --disable-ipfs --disable-manual \
      --disable-docs --disable-alt-svc --disable-hsts --disable-websockets \
      >/dev/null
    "$MAKE" -j"$JOBS" -C lib >/dev/null
    "$MAKE" -C lib install >/dev/null
    "$MAKE" -C include install >/dev/null
  )
  # git itself uses its built-in SHA code; only curl links OpenSSL.
  MAKE_FLAGS+=(
    "NO_OPENSSL=YesPlease"
    "CURL_CFLAGS=-I$DEPS/include"
    "CURL_LDFLAGS=-L$DEPS/lib -lcurl -lssl -lcrypto -lz -ldl -lpthread"
  )
fi
MAKE_FLAGS+=("CFLAGS=$GIT_CFLAGS" "LDFLAGS=$GIT_LDFLAGS")
if [ -n "${GIT_EXTRA_MAKE_FLAGS:-}" ]; then
  # shellcheck disable=SC2206  # deliberate word split of extra make variables
  MAKE_FLAGS+=(${GIT_EXTRA_MAKE_FLAGS})
fi

log "building git $GIT_VERSION"
fetch "$GIT_URL" "$GIT_SHA256" "$WORK/git.tar.xz"
tar -xJf "$WORK/git.tar.xz" -C "$WORK"
SRC="$WORK/git-$GIT_VERSION"
"$MAKE" -C "$SRC" -j"$JOBS" "${MAKE_FLAGS[@]}" git git-remote-http >"$WORK/git-build.log" 2>&1 || {
  tail -40 "$WORK/git-build.log" >&2
  die "git build failed (log: $WORK/git-build.log)"
}

EXEC="$OUT/libexec/git-core"
TEMPLATES="$OUT/share/git-core/templates"
mkdir -p "$EXEC" "$TEMPLATES/info"
cp "$SRC/git" "$EXEC/git"
cp "$SRC/git-remote-http" "$EXEC/git-remote-https"
cp "$SRC/templates/description" "$TEMPLATES/description"
cp "$SRC/templates/info/exclude" "$TEMPLATES/info/exclude"
chmod 0755 "$EXEC/git" "$EXEC/git-remote-https"
chmod 0644 "$TEMPLATES/description" "$TEMPLATES/info/exclude"
if [ -n "$STRIP" ]; then
  # shellcheck disable=SC2086  # STRIP may carry flags
  $STRIP "$EXEC/git" "$EXEC/git-remote-https"
fi
cp "$SRC/COPYING" "$OUT/COPYING"

# --- verification -----------------------------------------------------------
if find "$OUT" -type l | grep -q .; then
  die "symlinks in the bundled git tree"
fi
# An inherited GIT_EXEC_PATH (another git's helpers) would mask the relative
# lookup this checks; the CLIO resolver clears it the same way.
run_bundled() { env -u GIT_EXEC_PATH -u GIT_TEMPLATE_DIR "$EXEC/git" "$@"; }
run_bundled --version >/dev/null || die "bundled git does not run"
EXEC_PATH="$(run_bundled --exec-path)"
[ "$EXEC_PATH" = "$EXEC" ] || die "exec-path is $EXEC_PATH, expected $EXEC (RUNTIME_PREFIX not effective)"
if [ "$OS" = "Darwin" ] && command -v otool >/dev/null 2>&1 && xcode-select -p >/dev/null 2>&1; then
  for bin in "$EXEC/git" "$EXEC/git-remote-https"; do
    foreign="$(otool -L "$bin" | tail -n +2 | awk '{print $1}' | grep -v '^/usr/lib/' || true)"
    [ -z "$foreign" ] || die "$bin links outside /usr/lib: $foreign"
  done
fi
if [ "$OS" = "Linux" ]; then
  for bin in "$EXEC/git" "$EXEC/git-remote-https"; do
    foreign="$(ldd "$bin" | grep -Ev 'linux-vdso|ld-linux|libc\.so|libm\.so|libz\.so|libpthread\.so|libdl\.so|librt\.so' || true)"
    [ -z "$foreign" ] || die "$bin links unexpected libraries: $foreign"
  done
fi

log "OK: git $GIT_VERSION at $OUT ($(du -sk "$OUT" | awk '{print $1}') KB)"
