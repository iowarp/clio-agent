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
#   {"schema": 1, "exec": ["python/bin/python3.14", "-I", "-B", "-m", "clio_agent.gact", "--no-agent"]}
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
#   CLIO_RUNTIME_PYTHON  python minor version (default: 3.14, standard GIL build)
#   CLIO_BUNDLED_GIT     a git tree already built by build-bundled-git.sh to ship
#                        instead of building one (default: build it)
#
# Usage:
#   ./build-gact-runtime.sh <output-dir>
#   (the caller decides where; clio-bundles.yml passes the gact-tui
#    submodule's src-tauri/gact-runtime)

set -euo pipefail

OUT="${1:?usage: build-gact-runtime.sh <output-dir>}"
REF="${CLIO_REF:-develop}"
PYVER="${CLIO_RUNTIME_PYTHON:-3.14}"
REPO_URL="git+https://github.com/iowarp/clio-agent.git"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
# (cpython-<minor>-... -> cpython-<patch>-...). Copy the real one.
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
BUNDLE_EXTRAS="argonne desktop flowcept"
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
# uvx is uv's own small shim that execs the sibling uv as `uv tool run`; a copy
# of uv renamed uvx still behaves as plain uv (`uvx pkg` -> `uv pkg`).
UVX="$(dirname "$(command -v uv)")/uvx"
[ -x "$UVX" ] || { echo "build-gact-runtime: uvx shim missing beside uv" >&2; exit 1; }
cp "$UVX" "$OUT/bin/uvx"
chmod +x "$OUT/bin/uv" "$OUT/bin/uvx"

# CLIO installs marketplace plugins with git, and a fresh Mac has only the
# /usr/bin/git placeholder that opens the developer-tools installer. Ship a
# relocatable git (clio_agent/runtime/git_executable.py resolves it).
if [ -n "${CLIO_BUNDLED_GIT:-}" ]; then
  [ -x "${CLIO_BUNDLED_GIT}/libexec/git-core/git" ] || {
    echo "build-gact-runtime: CLIO_BUNDLED_GIT=$CLIO_BUNDLED_GIT has no libexec/git-core/git" >&2
    exit 1
  }
  echo "[build-gact-runtime] shipping prebuilt git from $CLIO_BUNDLED_GIT"
  cp -R "$CLIO_BUNDLED_GIT" "$OUT/git"
else
  echo "[build-gact-runtime] building the bundled git"
  "$SCRIPT_DIR/build-bundled-git.sh" "$OUT/git"
fi

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
  # Files no run of CLIO reads (about 1,100 in 0.9.5.1): every one is another file
  # to write, scan for malware and sign on install. Nested test suites (numpy's
  # alone is about 490), the C/C++ headers for compiling against numpy or pyarrow,
  # and the stdlib's IDLE and turtle demos. Same list in build-gact-runtime.ps1.
  # Type stubs (.pyi) stay: globus_sdk reads its own at import, and griffe
  # merges them into FastMCP tool docs.
  find "$SITE_PKGS" -mindepth 3 -type d \( -name tests -o -name test \) -prune \
    -exec rm -rf {} + 2>/dev/null || true
  rm -rf "$SITE_PKGS/pyarrow/include" "$SITE_PKGS/numpy/_core/include"
  rm -rf "$(dirname "$SITE_PKGS")/idlelib" "$(dirname "$SITE_PKGS")/turtledemo"
  # *.dist-info/RECORD is KEPT: the desktop upgrades this runtime in place
  # with `uv pip install`, and without RECORD uv cannot uninstall the old
  # version — stale modules and duplicate metadata survive the upgrade.

  # Installer-hostile filenames (NSIS aborts on parens/brackets — the
  # litellm benchmark-data lesson from the 0.7.0 gact-tui release).
  rm -rf "$SITE_PKGS/litellm/proxy/guardrails/guardrail_hooks/litellm_content_filter/guardrail_benchmarks" || true
  # Google's discovery documents for every Google API (107 MB in 0.9.5.1): CLIO builds
  # only the Drive v3 client (gact/storage/drive_filesystem.py), read from this cache.
  if [ -d "$SITE_PKGS/googleapiclient/discovery_cache/documents" ]; then
    find "$SITE_PKGS/googleapiclient/discovery_cache/documents" -type f ! -name 'drive.v3.json' -delete
    [ -f "$SITE_PKGS/googleapiclient/discovery_cache/documents/drive.v3.json" ] \
      || { echo "build-gact-runtime: googleapiclient has no drive.v3.json to keep" >&2; exit 1; }
  fi
  # Codex's exec-server and code-mode-host helpers (126 MB raw, 31 MB of the macOS
  # download): CLIO runs only `codex sandbox`, its macOS write fence, which was verified
  # live to enforce without them. Linux keeps them until a Linux run proves the same.
  CODEX_BIN="$SITE_PKGS/codex_cli_bin/bin"
  if [ "$(uname -s)" = Darwin ] && [ -d "$CODEX_BIN" ]; then
    [ -f "$CODEX_BIN/codex" ] \
      || { echo "build-gact-runtime: codex_cli_bin has no codex binary to keep" >&2; exit 1; }
    rm -f "$CODEX_BIN/codex-exec-server" "$CODEX_BIN/codex-code-mode-host"
    for record in "$SITE_PKGS"/openai_codex_cli_bin-*.dist-info/RECORD; do
      [ -f "$record" ] || continue
      grep -v -E '^codex_cli_bin/bin/codex-(exec-server|code-mode-host),' "$record" >"$record.tmp"
      mv "$record.tmp" "$record"
    done
  fi
  # Playwright's Node.js (122 MB raw, 28 MB of the macOS download) is fetched, hash-checked,
  # the first time the agent opens a browser (gact/browser/driver.py); same step on Windows.
  "$OUT/$PYBIN_REL" -I -B "$SCRIPT_DIR/stage_playwright_node.py" "$SITE_PKGS"
  # pip, C/Cython build sources and dist-info markers, the same on every OS.
  "$OUT/$PYBIN_REL" -I -B "$SCRIPT_DIR/prune_runtime.py" "$SITE_PKGS"
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

echo "[build-gact-runtime] preparing locked document wheels"
DOCUMENT_STACK="$(find "$OUT/python" -path '*/site-packages/clio_agent/runtime/document_stack' -type d -print -quit)"
test -n "$DOCUMENT_STACK"
"$OUT/$PYBIN_REL" -I -B "$CHECKOUT/install/build_document_wheels.py" \
  --stack "$DOCUMENT_STACK" --out "$OUT/document-wheels" --uv "$(command -v uv)"

# The pinned default marketplace, installed here once: an install adopts this store
# instead of fetching, reading and importing ~3,500 files on the person's machine
# (clio_agent/plugins/prebuilt.py; the same step on every OS).
echo "[build-gact-runtime] installing the default marketplace"
"$OUT/$PYBIN_REL" -I -B -m clio_agent.plugins.prebuilt build --out "$OUT/marketplace"

# --- 4. generic runtime manifest ----------------------------------------
# `source` names the commit this runtime was built from and whether it is a
# release tag: a CLIO built from any other commit deploys that commit to remote
# hosts instead of the PyPI release with the same version number
# (gact/infrastructure/controller_source.py).
SOURCE_COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD 2>/dev/null || true)"
SOURCE_RELEASE=false
case "$(git -C "$CHECKOUT" describe --exact-match --tags HEAD 2>/dev/null || true)" in v*) SOURCE_RELEASE=true ;; esac
cat >"$OUT/runtime.json" <<EOF
{
  "schema": 1,
  "exec": ["${PYBIN_REL}", "-I", "-B", "-m", "clio_agent.gact", "--no-agent"],
  "env": {"PYTHONDONTWRITEBYTECODE": "1"},
  "source": {"commit": "${SOURCE_COMMIT}", "release": ${SOURCE_RELEASE}}
}
EOF
echo "[build-gact-runtime] manifest: $(cat "$OUT/runtime.json" | tr -d '\n' | tr -s ' ')"

# Prepare the real launch's bytecode in the release image, not on the user's
# first launch: the startup graph, the first-party packages, the standard
# library, and every module a real boot of this image imports (--launch-path
# boots it, so it runs after runtime.json exists). Compiling all of
# site-packages is both wasteful and invalid: some optional provider paths
# exceed Windows' legacy path limit.
echo "[build-gact-runtime] compiling portable startup bytecode"
"$OUT/$PYBIN_REL" -I -B "$CHECKOUT/install/precompile_runtime.py" \
  --python-root "$OUT/python" --package clio_agent --package clio_kit --package clio_schemas \
  --stdlib --launch-path
COMPILED="$(find "$OUT/python" -type f -name '*.pyc' | wc -l | tr -d ' ')"
if [ "${COMPILED:-0}" -eq 0 ]; then
  echo "build-gact-runtime: bytecode preparation produced no .pyc files" >&2
  exit 1
fi
echo "[build-gact-runtime] prepared $COMPILED bytecode files"

# The standard library's pure-Python modules import from pythonXY.zip, one file
# instead of about 1,100 that every install writes (gact-tui#739).
"$OUT/$PYBIN_REL" -I -B "$SCRIPT_DIR/zip_stdlib.py"

SIZE_AFTER="$(dir_size_mb "$OUT")"
echo "[build-gact-runtime] size after prune:  ${SIZE_AFTER} MB (was ${SIZE_BEFORE} MB)"

# The one-shot upgrade repair writes a fingerprint marker next to Python.
# Prepare it in the ORIGINAL image before macOS seals the app; booting only
# the relocated copy leaves first launch modifying a signed resource tree.
PYTHONDONTWRITEBYTECODE=1 "$OUT/$PYBIN_REL" -I -B - <<'PY'
import sys
from pathlib import Path
from clio_agent.gact.runtime_bytecode_repair import repair_bundled_runtime_bytecode

report = repair_bundled_runtime_bytecode()
if report is not None and (report.errors or report.ambiguous_metadata):
    raise SystemExit("build-gact-runtime: bundled bytecode verification failed")
if not (Path(sys.prefix) / ".clio-bytecode-verified").is_file():
    raise SystemExit("build-gact-runtime: bundled bytecode marker was not prepared")
PY

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
"$RELOC/$PYBIN_REL" -I -B -m clio_agent.gact --help >/dev/null
"$RELOC/$PYBIN_REL" -I -B -c 'from clio_kit import cli; cli()' --help >/dev/null
"$RELOC/bin/uv" --version >/dev/null
# The bundled git must work with no git on PATH and nothing inherited, through
# the same resolver CLIO uses (it also supplies Linux's CA bundle).
echo "[build-gact-runtime] sanity (relocated): bundled git ls-remote with an empty PATH"
GIT_HOME="$(mktemp -d)"
env -i HOME="$GIT_HOME" PATH=/var/empty "$RELOC/$PYBIN_REL" -I -B - <<'PY'
from clio_agent.runtime.git_executable import resolve_git, run_git

git = resolve_git()
if git.source != "bundled":
    raise SystemExit(f"build-gact-runtime: resolver chose {git.source} git {git.path}")
run_git(
    ["ls-remote", "https://github.com/iowarp/clio-agent-marketplace.git", "HEAD"],
    check=True,
    capture_output=True,
    timeout=120,
)
print(f"[build-gact-runtime] bundled git OK: {git.path}")
PY
rm -rf "$GIT_HOME"
# --help only proves imports; BOOT the relocated copy and poll the API —
# the only automated proof a prune casualty or loader problem would fail.
PORT=$((RANDOM % 20000 + 24000))
echo "[build-gact-runtime] sanity (relocated boot): /v1/capabilities on :$PORT"
"$RELOC/$PYBIN_REL" -I -B -m clio_agent.gact --no-agent --host 127.0.0.1 --port "$PORT" >/dev/null 2>&1 &
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
