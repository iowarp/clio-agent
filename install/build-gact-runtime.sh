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
#   {"schema": 1, "exec": ["python/bin/python3.12", "-m", "clio_agent.gact", "--no-agent"]}
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

# --- 2. install clio-agent + the portable science launcher -------------
# CHECKOUT always ends up a local clio-agent tree with its OWN pyproject.toml +
# uv.lock -- CLIO_AGENT_SOURCE directly, or a fresh clone of CLIO_REF otherwise
# -- so step 2b can export that EXACT lock as a constraint file. Without this,
# `uv pip install <spec> ...` below is an unlocked resolve against whatever is
# on PyPI at build time: the incident this fixes shipped iowarp-core 2.2.1 in a
# bundle while uv.lock (and CI) were still on 2.1.0.
CLEANUP_CHECKOUT=""
if [ -n "${CLIO_AGENT_SOURCE:-}" ]; then
  [ -f "${CLIO_AGENT_SOURCE}/pyproject.toml" ] || {
    echo "build-gact-runtime: CLIO_AGENT_SOURCE=$CLIO_AGENT_SOURCE is not a clio-agent checkout" >&2
    exit 1
  }
  CHECKOUT="${CLIO_AGENT_SOURCE}"
  SPEC="${CHECKOUT}"
else
  CHECKOUT="$(mktemp -d)/clio-agent-ref-checkout"
  CLEANUP_CHECKOUT="$CHECKOUT"
  echo "[build-gact-runtime] cloning clio-agent@$REF to export its lock"
  git clone --quiet --depth 1 --branch "$REF" "${REPO_URL#git+}" "$CHECKOUT"
  SPEC="${CHECKOUT}"
fi
trap '[ -z "$CLEANUP_CHECKOUT" ] || rm -rf "$(dirname "$CLEANUP_CHECKOUT")"' EXIT

# --- 2b. export uv.lock as a constraint so the resolve below cannot drift ---
# BUNDLE_EXTRAS is THE bundle's install set, defined once: the export below and
# the install after it both use it, so the constraint file covers every package
# the install can pull in (an extra missing from the export resolves unpinned --
# how pytz drifted off the lock). Everything the bundle ships beyond base deps
# lives in the `desktop` extra of pyproject.toml, never as a pin in this script:
# a separate pin is resolved against the lock only at build time, which is how a
# hardcoded clio-kit==2.10.6 (click>=8.3.3) broke against the locked click.
# scripts/check_bundle_matches_lock.py BUNDLE_EXTRAS must equal this list
# (tests/test_scripts/test_check_bundle_matches_lock.py enforces it).
BUNDLE_EXTRAS="argonne desktop"
CONSTRAINTS="$OUT/.lock-constraints.txt"
EXPORT_EXTRA_ARGS=""
for extra in $BUNDLE_EXTRAS; do EXPORT_EXTRA_ARGS="$EXPORT_EXTRA_ARGS --extra $extra"; done
echo "[build-gact-runtime] exporting $CHECKOUT/uv.lock (extras: $BUNDLE_EXTRAS) as an install constraint"
# shellcheck disable=SC2086  # EXPORT_EXTRA_ARGS is a deliberate word list
uv export --project "$CHECKOUT" --frozen --no-hashes --no-emit-project $EXPORT_EXTRA_ARGS \
  -o "$CONSTRAINTS" 2>&1 | tail -5
[ -s "$CONSTRAINTS" ] || { echo "build-gact-runtime: uv export produced no constraints" >&2; exit 1; }

BUNDLE_SPEC="${SPEC}[$(echo "$BUNDLE_EXTRAS" | tr ' ' ',')]"
echo "[build-gact-runtime] installing: $BUNDLE_SPEC (locked)"
uv pip install --python "$OUT/$PYBIN_REL" --constraint "$CONSTRAINTS" "$BUNDLE_SPEC"

# Install the source-locked Web Search MCP adapter now.  Connecting the
# recommended service must not build a second Python environment on first use.
# Its dependencies are already installed from the `desktop` extra; the
# constraint keeps any it adds on the lock (and check_bundle_matches_lock.py
# fails on a package the lock does not cover).
WEB_MCP_PROJECT="$OUT/python/clio-kit-mcp-servers/web"
[ -f "$WEB_MCP_PROJECT/pyproject.toml" ] || {
  echo "build-gact-runtime: bundled Web Search MCP project missing at $WEB_MCP_PROJECT" >&2
  exit 1
}
echo "[build-gact-runtime] installing bundled CLIO Web Search adapter"
uv pip install --python "$OUT/$PYBIN_REL" --constraint "$CONSTRAINTS" "$WEB_MCP_PROJECT"
rm -f "$CONSTRAINTS"

# check_bundle_matches_lock.py is the automated proof this constraint actually
# took effect (release-time backstop, not a substitute for it) -- run from the
# CI workflow right after this script, against $CHECKOUT/uv.lock (the exact
# tree this runtime was built from).

# clio-kit materializes each locked MCP server with uv on first use. Ship uv
# beside the relocatable runtime instead of requiring a fresh desktop user to
# install developer tooling or configure PATH.
mkdir -p "$OUT/bin"
cp "$(command -v uv)" "$OUT/bin/uv"
cp "$(command -v uv)" "$OUT/bin/uvx"
chmod +x "$OUT/bin/uv" "$OUT/bin/uvx"

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
  # *.dist-info/RECORD is KEPT: the desktop upgrades this runtime in place
  # with `uv pip install`, and without RECORD uv cannot uninstall the old
  # version — stale modules and duplicate metadata survive the upgrade.

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

# Prepare the real startup import graph in the release image, not on the
# user's first launch. Compiling the entire distribution is both wasteful and
# invalid: CPython ships non-imported Tcl demo files with syntax errors, while
# some optional provider paths exceed Windows' legacy path limit.
echo "[build-gact-runtime] compiling portable startup bytecode"
"$OUT/$PYBIN_REL" "$CLIO_AGENT_SOURCE/install/precompile_runtime.py" \
  --python-root "$OUT/python"
COMPILED="$(find "$OUT/python" -type f -name '*.pyc' | wc -l | tr -d ' ')"
if [ "${COMPILED:-0}" -eq 0 ]; then
  echo "build-gact-runtime: bytecode preparation produced no .pyc files" >&2
  exit 1
fi
echo "[build-gact-runtime] prepared $COMPILED bytecode files"

SIZE_AFTER="$(dir_size_mb "$OUT")"
echo "[build-gact-runtime] size after prune:  ${SIZE_AFTER} MB (was ${SIZE_BEFORE} MB)"

# --- 4. generic runtime manifest ----------------------------------------
cat >"$OUT/runtime.json" <<EOF
{
  "schema": 1,
  "exec": ["${PYBIN_REL}", "-m", "clio_agent.gact", "--no-agent"]
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
"$RELOC/$PYBIN_REL" -c 'from clio_kit import cli; cli()' --help >/dev/null
"$RELOC/bin/uv" --version >/dev/null
# --help only proves imports; BOOT the relocated copy and poll the API —
# the only automated proof a prune casualty or loader problem would fail.
PORT=$((RANDOM % 20000 + 24000))
echo "[build-gact-runtime] sanity (relocated boot): /v1/capabilities on :$PORT"
"$RELOC/$PYBIN_REL" -m clio_agent.gact --no-agent --host 127.0.0.1 --port "$PORT" >/dev/null 2>&1 &
SRV=$!
BOOT_STARTED="$(date +%s)"
BOOT_OK=""
for _ in $(seq 1 30); do
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
  echo "build-gact-runtime: relocated runtime failed to serve /v1/capabilities within 30 seconds" >&2
  exit 1
fi
echo "[build-gact-runtime] relocated cold boot ready in $(( $(date +%s) - BOOT_STARTED ))s"

echo "[build-gact-runtime] OK — portable runtime ready at $OUT (${SIZE_AFTER} MB)"
