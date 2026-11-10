#!/usr/bin/env bash
# Compress the portable CLIO runtime into the two resources the Linux deb and rpm ship,
# the same pair install/pack-gact-runtime.ps1 makes for Windows: gact-runtime.tar.zst
# (install/compress_runtime.py: level 19, long window) and gact-runtime.pack.json.
#
# dpkg writes, fsyncs and records each of a package's files one by one: the exploded
# runtime was 32,250 of them. The package now carries one archive, and its maintainer
# script (install/linux-package/postinst) verifies and unpacks it in one pass.
#
#   bash install/pack-gact-runtime.sh [RUNTIME] [ARCHIVE] [MANIFEST]
#
# It also places that script beside the manifest as gact-runtime.unpack.sh, which the
# package ships so install.sh can finish an rpm upgrade (see the script's header).
set -euo pipefail

die() { echo "pack-gact-runtime: $*" >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
tauri="$here/../external/gact-tui/desktop/src-tauri"
runtime="${1:-$tauri/gact-runtime}"
archive="${2:-$tauri/gact-runtime.tar.zst}"
manifest="${3:-$tauri/gact-runtime.pack.json}"

[ -f "$runtime/runtime.json" ] || die "$runtime does not contain runtime.json"
# The maintainer script unpacks the archive's gact-runtime/ beside the manifest.
[ "$(basename "$runtime")" = gact-runtime ] || die "the runtime directory must be named gact-runtime"
[ "$(basename "$archive")" = gact-runtime.tar.zst ] || die "the archive must be named gact-runtime.tar.zst"
python="$runtime/python/bin/python3.14"
[ -x "$python" ] || die "$python is missing"
# Startup bytecode is prepared at build time (install/precompile_runtime.py); the root-owned
# runtime cannot write it later, so an archive without it would compile on every launch.
[ -n "$(find "$runtime/python" -type f -name '*.pyc' -print -quit)" ] \
  || die "$runtime carries no precompiled bytecode; run install/build-gact-runtime.sh"

mkdir -p "$(dirname "$archive")" "$(dirname "$manifest")"
rm -f "$archive" "$manifest"
cp "$here/linux-package/postinst" "$(dirname "$manifest")/gact-runtime.unpack.sh"
plain="$archive.tar"
rm -f "$plain"
trap 'rm -f "$plain"' EXIT

start=$SECONDS
echo "[pack-gact-runtime] archiving $runtime"
# Root-owned and not group- or world-writable once unpacked, as dpkg installed it.
# bsdtar (macOS) has no --mode; it only runs there for local checks.
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
  owner=(--owner=0 --group=0 --numeric-owner --sort=name --mode=go-w)
else
  owner=(--uid 0 --gid 0)
fi
tar "${owner[@]}" -cf "$plain" -C "$(dirname "$runtime")" gact-runtime
"$python" -I -B "$here/compress_runtime.py" --tar "$plain" --out "$archive"
rm -f "$plain"

"$python" -I -B - "$runtime" "$archive" "$manifest" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

runtime, archive, manifest = (Path(arg) for arg in sys.argv[1:4])
files = 0
size = 0
for root, _dirs, names in os.walk(runtime):
    for name in names:
        info = os.lstat(os.path.join(root, name))
        if not os.path.islink(os.path.join(root, name)):
            files += 1
            size += info.st_size
digest = hashlib.sha256()
with archive.open("rb") as stream:
    while chunk := stream.read(4 << 20):
        digest.update(chunk)
pack = {
    "schema": 1,
    "archive": archive.name,
    "sha256": digest.hexdigest(),
    "runtime_files": files,
    "runtime_bytes": size,
    "archive_bytes": archive.stat().st_size,
}
manifest.write_text(json.dumps(pack, separators=(",", ":")) + "\n", encoding="utf-8")
print(
    f"[pack-gact-runtime] OK - {size / 2**20:.1f} MiB -> {pack['archive_bytes'] / 2**20:.1f} MiB,"
    f" {files} files; SHA-256 {pack['sha256']}"
)
PY
echo "[pack-gact-runtime] done in $((SECONDS - start))s"
