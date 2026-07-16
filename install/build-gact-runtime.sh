#!/usr/bin/env bash
# Build the PORTABLE embedded clio-agent runtime for the bundled CLIO
# Desktop installer variant (macOS / Linux). Windows: build-gact-runtime.ps1.
#
# Replaces gact-tui's retired build-clio-runtime.sh (#909): that script
# built a `uv venv --relocatable`, whose python is a shim loading the
# BUILD HOST's base interpreter from pyvenv.cfg `home` — so the shipped
# runtime could never start on a fresh machine. This script instead ships
# the interpreter itself: uv's python-build-standalone distribution is
# copied INTO the runtime and the clio-agent wheel is installed directly
# into it (no venv, no pyvenv.cfg, no build-host paths on the exec path).
#
# The runtime self-describes via a generic manifest (<out>/runtime.json,
# iowarp/gact-tui#311) so the desktop launcher needs zero knowledge of
# what's inside:
#   {"schema": 1, "exec": ["python/bin/python3.12", "-m", "clio_agent.gact"]}
#
# Console scripts are DELETED after install: their shims embed absolute
# build paths and break on relocation — `-m clio_agent.gact` is the only
# supported entry. The build proves portability on the real object: the
# finished tree is copied to a temp location and booted from there.
#
# Env:
#   CLIO_REF             git ref of clio-agent to install (default: develop)
#   CLIO_AGENT_SOURCE    local clio-agent checkout to install from instead
#                        of the git ref (CI passes its own workspace so the
#                        runtime is built from EXACTLY the released tree)
#   CLIO_RUNTIME_PYTHON  python minor version (default: 3.12)
#
# Usage:
#   ./build-gact-runtime.sh <output-dir>
#   (the caller decides where; clio-bundles.yml passes the gact-tui
#    submodule's src-tauri/gact-runtime)

set -euo pipefail

OUT="${1:?usage: build-gact-runtime.sh <output-dir>}"
REF="${CLIO_REF:-develop}"
PYVER="${CLIO_RUNTIME_PYTHON:-3.12}"
REPO_URL="git+https://github.com/iowarp/clio-agent.git"

dir_size_mb() {
  if [ ! -d "$1" ]; then echo "0"; return; fi
  du -sm "$1" 2>/dev/null | awk '{print $1}'
}

command -v uv >/dev/null 2>&1 || {
  echo "build-gact-runtime: 'uv' is required but not found on PATH." >&2
  exit 1
}
echo "[build-gact-runtime] uv: $(command -v uv) ($(uv --version))"

# Always rebuild from clean so a stale tree can't leak into the bundle.
if [ -d "$OUT" ]; then
  echo "[build-gact-runtime] removing existing $OUT before rebuild"
  rm -rf "$OUT"
fi
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

# --- 1. interpreter: python-build-standalone, copied INTO the runtime ---
STAGING="$OUT/.uv-python-staging"
echo "[build-gact-runtime] installing standalone CPython $PYVER"
uv python install "$PYVER" --install-dir "$STAGING"
# The staging dir holds the real versioned dist plus a bare-minor alias
# (cpython-3.12-... -> cpython-3.12.13-...). Copy the real one.
DIST="$(find "$STAGING" -maxdepth 1 -type d -name "cpython-${PYVER}.[0-9]*" | head -1)"
[ -n "$DIST" ] || { echo "build-gact-runtime: no cpython dist under $STAGING" >&2; exit 1; }
cp -a "$DIST" "$OUT/python"
rm -rf "$STAGING"

# Our copy is a private distribution now, not uv's managed install —
# drop the PEP 668 marker so `uv pip install --python` targets it.
find "$OUT/python" -maxdepth 3 -name EXTERNALLY-MANAGED -delete

PYBIN_REL="python/bin/python${PYVER}"
[ -x "$OUT/$PYBIN_REL" ] || { echo "build-gact-runtime: $PYBIN_REL missing in dist" >&2; exit 1; }

# --- 2. install clio-agent (NO extras) directly into the dist ----------
if [ -n "${CLIO_AGENT_SOURCE:-}" ]; then
  [ -f "${CLIO_AGENT_SOURCE}/pyproject.toml" ] || {
    echo "build-gact-runtime: CLIO_AGENT_SOURCE=$CLIO_AGENT_SOURCE is not a clio-agent checkout" >&2
    exit 1
  }
  SPEC="${CLIO_AGENT_SOURCE}"
else
  SPEC="clio-agent @ ${REPO_URL}@${REF}"
fi
echo "[build-gact-runtime] installing: $SPEC (no extras)"
uv pip install --prerelease allow --python "$OUT/$PYBIN_REL" "$SPEC"

SIZE_BEFORE="$(dir_size_mb "$OUT")"
echo "[build-gact-runtime] size before prune: ${SIZE_BEFORE} MB"

# --- 3. prune -----------------------------------------------------------
find "$OUT/python" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$OUT/python" -type f -name '*.pyc' -delete 2>/dev/null || true

SITE_PKGS="$(echo "$OUT"/python/lib/python*/site-packages)"
if [ -d "$SITE_PKGS" ]; then
  # in-package tests/ trees in vendored deps (clio_agent ships none)
  find "$SITE_PKGS" -mindepth 2 -maxdepth 2 -type d \( -name tests -o -name test \) \
    -exec rm -rf {} + 2>/dev/null || true
  # *.dist-info/RECORD bloat (not needed at runtime)
  find "$SITE_PKGS" -mindepth 2 -maxdepth 2 -type f -path '*.dist-info/RECORD' \
    -delete 2>/dev/null || true

  # Installer-hostile filenames (NSIS aborts on parens/brackets — the
  # litellm benchmark-data lesson from the 0.7.0 gact-tui release).
  rm -rf "$SITE_PKGS/litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/guardrail_benchmarks" || true
  find "$OUT/python" -type f \( -name '*(*' -o -name '*)*' -o -name '*\[*' -o -name '*\]*' \) -delete 2>/dev/null || true
  remaining="$(find "$OUT/python" -type f \( -name '*(*' -o -name '*)*' -o -name '*\[*' -o -name '*\]*' \) | head -20)"
  if [ -n "$remaining" ]; then
    echo "build-gact-runtime: installer-hostile filenames remain after prune:" >&2
    echo "$remaining" >&2
    exit 1
  fi
fi

# Console-script shims embed the absolute build path — relocation traps.
# Delete every non-interpreter entry in bin/ (regular files AND symlinks:
# deleting 2to3-3.12 while leaving the 2to3 symlink dangling broke Tauri's
# resource walk on the v0.7.1 unix legs); -m is the only entry.
# python*-config (file OR symlink) embeds build prefixes — same trap; the
# v0.7.2 macOS leg died on the python3-config symlink left dangling.
find "$OUT/python/bin" -maxdepth 1 \( -type f -o -type l \) ! -name 'python*' -delete
find "$OUT/python/bin" -maxdepth 1 \( -type f -o -type l \) -name 'python*-config' -delete
# Windows launcher stubs vendored by distlib/setuptools inside site-packages
# (t64.exe, w64-arm.exe, ...): dead weight on unix, and the release staging
# sweeps *.exe as installers — a 180KB t64-arm.exe masqueraded as the bundled
# installer and failed the 60MB payload floor on the v0.7.2 linux legs.
find "$OUT/python" -type f -name '*.exe' -delete
# Sweep any dangling symlink left anywhere in the dist (resource walkers fail
# on them). PORTABLE: BSD find has no -xtype (the GNU-only sweep silently
# no-opped on macOS — the exact silent-fallback class this repo bans).
find "$OUT/python" -type l ! -exec test -e {} ';' -delete

SIZE_AFTER="$(dir_size_mb "$OUT")"
echo "[build-gact-runtime] size after prune:  ${SIZE_AFTER} MB (was ${SIZE_BEFORE} MB)"

# --- 4. generic runtime manifest ----------------------------------------
cat >"$OUT/runtime.json" <<EOF
{
  "schema": 1,
  "exec": ["${PYBIN_REL}", "-m", "clio_agent.gact"]
}
EOF
echo "[build-gact-runtime] manifest: $(cat "$OUT/runtime.json" | tr -d '\n' | tr -s ' ')"

# --- 5. portability proof on the real object ----------------------------
# A venv would leave a pyvenv.cfg pinning the build host's interpreter;
# assert the failure mode is structurally absent, then boot the runtime
# FROM A RELOCATED COPY — the invariant the old script never proved.
if find "$OUT" -name pyvenv.cfg | grep -q .; then
  echo "build-gact-runtime: pyvenv.cfg found — runtime is venv-shaped, not portable" >&2
  exit 1
fi
RELOC="$(mktemp -d)/gact-runtime-relocated"
cp -a "$OUT" "$RELOC"
echo "[build-gact-runtime] sanity (relocated): $RELOC/$PYBIN_REL -m clio_agent.gact --help"
"$RELOC/$PYBIN_REL" -m clio_agent.gact --help >/dev/null
# --help only proves imports; BOOT the relocated copy and poll the API —
# the only automated proof a prune casualty or loader problem would fail.
PORT=$((RANDOM % 20000 + 24000))
echo "[build-gact-runtime] sanity (relocated boot): /v1/capabilities on :$PORT"
"$RELOC/$PYBIN_REL" -m clio_agent.gact --no-agent --host 127.0.0.1 --port "$PORT" >/dev/null 2>&1 &
SRV=$!
BOOT_OK=""
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/v1/capabilities" >/dev/null 2>&1; then
    BOOT_OK=1
    break
  fi
  sleep 1
done
kill "$SRV" 2>/dev/null || true
wait "$SRV" 2>/dev/null || true
rm -rf "$(dirname "$RELOC")"
if [ -z "$BOOT_OK" ]; then
  echo "build-gact-runtime: relocated runtime failed to serve /v1/capabilities" >&2
  exit 1
fi

echo "[build-gact-runtime] OK — portable runtime ready at $OUT (${SIZE_AFTER} MB)"
