#!/bin/bash
# Step 01: code, tools and builds on project storage. Idempotent: each part is skipped when
# its recorded identity (commit / lock digest) is unchanged. Runs on a login node (direct
# internet) or a compute node (through the ALCF proxy).
#   uv + Node + pnpm (pinned, sha256-checked) -> clio-agent @ BRANCH with its submodules
#   (gact-tui, clio-agent-marketplace) + clio-schemas @ BRANCH + experiments + connector
#   -> uv sync --frozen --extra dev --extra flowcept -> UI with the CLIO brand
#   -> attention connector wheel = CLIO's pin + the one-line build fix.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
STAGE="$EVIDENCE/stage"; run mkdir -p "$STAGE" "$TOOLS/bin" "$ROOT/cache"
details=()

fetch() {  # fetch URL OUT
  run curl -fsSL --retry 4 --retry-delay 5 -o "$2" "$1"
}

# --- uv --------------------------------------------------------------------------------
if [ "$("$TOOLS/bin/uv" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  log "installing uv $UV_VERSION"
  t="uv-x86_64-unknown-linux-gnu.tar.gz"; u="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$t"
  d="$NODE_TMP/uv-dl"; run mkdir -p "$d"
  fetch "$u" "$d/$t"; fetch "$u.sha256" "$d/$t.sha256"
  run bash -c "cd '$d' && sha256sum -c '$t.sha256' && tar -xzf '$t' && cp uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx '$TOOLS/bin/'"
fi
details+=("uv=\"$UV_VERSION\"")

# --- Node.js + pnpm (UI build only) -------------------------------------------------------
if [ "$("$TOOLS/node/bin/node" --version 2>/dev/null)" != "v$NODE_VERSION" ]; then
  log "installing Node $NODE_VERSION"
  t="node-v$NODE_VERSION-linux-x64.tar.xz"; d="$NODE_TMP/node-dl"; run mkdir -p "$d"
  fetch "https://nodejs.org/dist/v$NODE_VERSION/$t" "$d/$t"
  fetch "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" "$d/SHASUMS256.txt"
  run bash -c "cd '$d' && grep ' $t\$' SHASUMS256.txt | sha256sum -c - && tar -xJf '$t' -C '$TOOLS' && ln -sfn '$TOOLS/node-v$NODE_VERSION-linux-x64' '$TOOLS/node'"
fi
if [ "$("$TOOLS/pnpm/node_modules/.bin/pnpm" --version 2>/dev/null)" != "$PNPM_VERSION" ]; then
  run npm install --no-fund --no-audit --prefix "$TOOLS/pnpm" "pnpm@$PNPM_VERSION"
fi
details+=("node=\"$NODE_VERSION\"" "pnpm=\"$PNPM_VERSION\"")

# --- clio-kit (MCP launcher for the parquet/web/adios plugins; Desktop bundles it) ----------
if [ "$(uv tool list 2>/dev/null | awk '$1=="clio-kit"{print $2}')" != "v$CLIO_KIT_VERSION" ]; then
  run env UV_TOOL_BIN_DIR="$TOOLS/bin" uv tool install --force "clio-kit==$CLIO_KIT_VERSION"
fi
details+=("clio_kit=\"$CLIO_KIT_VERSION\"")

# --- repositories ---------------------------------------------------------------------------
# clone_at DIR URL REF: clone or fast-forward; a dirty checkout is left alone (recorded).
clone_at() {
  local dir="$1" url="$2" ref="$3"
  if [ ! -d "$dir/.git" ]; then
    run git clone --filter=blob:none "$url" "$dir"
  elif [ -n "$(git -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    log "$dir has local changes; not updating it"
    return 0
  fi
  run git -C "$dir" fetch --quiet origin "$ref" 2>/dev/null || run git -C "$dir" fetch --quiet origin
  if git -C "$dir" rev-parse --verify --quiet "origin/$ref" >/dev/null 2>&1; then
    # Fast-forward only: local commits ahead of the remote (an operator's fixes) are kept.
    if git -C "$dir" rev-parse --verify --quiet "refs/heads/$ref" >/dev/null 2>&1; then
      run git -C "$dir" checkout --quiet "$ref"
      run git -C "$dir" merge --quiet --ff-only "origin/$ref" \
        || log "WARNING: $dir $ref has diverged from origin/$ref; left as is"
    else
      run git -C "$dir" checkout --quiet -b "$ref" "origin/$ref"
    fi
  else
    run git -C "$dir" checkout --quiet --detach "$ref"
  fi
}
clone_at "$CLIO_SRC" "$GITHUB/iowarp/clio-agent.git" "$BRANCH"
run git -C "$CLIO_SRC" submodule sync --quiet
run git -C "$CLIO_SRC" submodule update --init --recursive
# CLIO installs clio-schemas from its uv.lock pin; this checkout only feeds the version check
# below. The branch may not be pushed to clio-schemas: then use its default branch, loudly.
SCHEMAS_REF="$BRANCH"
if ! git ls-remote --exit-code --heads "$GITHUB/iowarp/clio-schemas.git" "$BRANCH" >/dev/null 2>&1; then
  SCHEMAS_REF="$(git ls-remote --symref "$GITHUB/iowarp/clio-schemas.git" HEAD 2>/dev/null | awk '/^ref:/{sub("refs/heads/","",$2); print $2}')"
  SCHEMAS_REF="${SCHEMAS_REF:-main}"
  log "WARNING: clio-schemas has no branch $BRANCH; using $SCHEMAS_REF (CLIO's lock pin is what runs)"
fi
clone_at "$SRC/clio-schemas" "$GITHUB/iowarp/clio-schemas.git" "$SCHEMAS_REF"
details+=("clio_schemas_ref=\"$SCHEMAS_REF\"")
clone_at "$SRC/experiments" "$GITHUB/spotter-ai-genesis/experiments.git" "$EXPERIMENTS_REV"
clone_at "$SRC/vllm-attn-connector" "$GITHUB/spotter-ai-genesis/vllm-attn-connector.git" "$CONNECTOR_REV"

head_of() { git -C "$1" rev-parse HEAD 2>/dev/null || echo none; }
# The submodule gitlinks are the integrated build; also report whether they match the
# branch tips of the submodule repositories (a mismatch is reported, not "fixed").
for sub in external/gact-tui external/clio-agent-marketplace; do
  pinned=$(head_of "$CLIO_SRC/$sub")
  tip=$(git -C "$CLIO_SRC/$sub" ls-remote origin "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}' || true)
  details+=("$(basename "$sub")_pinned=\"$pinned\"" "$(basename "$sub")_branch_tip=\"${tip:-absent}\"")
  [ -n "$tip" ] && [ "$tip" != "$pinned" ] && log "WARNING: $sub pinned $pinned but $BRANCH tip is $tip"
done
details+=("clio_agent=\"$(head_of "$CLIO_SRC")\"" "clio_schemas=\"$(head_of "$SRC/clio-schemas")\""
          "experiments=\"$(head_of "$SRC/experiments")\"")
if [ "$DRY_RUN" != 1 ]; then
  # CLIO pins clio-schemas either by version (clio-schemas==X) or by commit (@ git+...@sha).
  lock_pin=$(grep -o '"clio-schemas[ =@][^"]*"' "$CLIO_SRC/pyproject.toml" | head -1 | tr -d '"' || true)
  branch_ver=$(grep -m1 '^version' "$SRC/clio-schemas/pyproject.toml" | cut -d'"' -f2 || true)
  details+=("clio_schemas_lock_pin=\"$lock_pin\"" "clio_schemas_branch_version=\"$branch_ver\"")
  case "$lock_pin" in
    clio-schemas==*) [ "${lock_pin#clio-schemas==}" = "$branch_ver" ] || log "WARNING: CLIO locks $lock_pin but the clio-schemas branch is $branch_ver" ;;
    *) log "CLIO pins $lock_pin (the lock is what runs)" ;;
  esac
  # CLIO's own connector pin must be the revision this kit patches.
  pin=$(grep -o 'CONNECTOR_REVISION = "[0-9a-f]*"' "$CLIO_SRC/src/clio_agent/gact/infrastructure/native_vllm.py" | cut -d'"' -f2)
  [ "$pin" = "$CONNECTOR_REV" ] || log "WARNING: CLIO pins connector $pin, kit builds $CONNECTOR_REV"
  details+=("clio_connector_pin=\"$pin\"")
fi

# --- CLIO environment -------------------------------------------------------------------------
stamp="$STAGE/uv-sync.sha256"
if [ "$DRY_RUN" = 1 ] || ! (cd "$CLIO_SRC" 2>/dev/null && sha256sum --status -c "$stamp" 2>/dev/null) || [ ! -x "$CLIO_SRC/.venv/bin/clio-agent" ]; then
  log "uv sync (frozen, extras dev + flowcept)"
  run bash -c "cd '$CLIO_SRC' && uv sync --frozen --extra dev --extra flowcept"
  run bash -c "cd '$CLIO_SRC' && sha256sum pyproject.toml uv.lock > '$stamp'"
fi
details+=("python=\"$("$CLIO_SRC/.venv/bin/python" -V 2>&1 || echo none)\"")

# --- UI with the CLIO brand -----------------------------------------------------------------------
brand_digest=$(cd "$CLIO_SRC/branding" 2>/dev/null && find . -type f -exec sha256sum {} + | sort | sha256sum | cut -c1-16 || echo none)
# Uncommitted UI changes rebuild too: the id carries a digest of the working-tree diff.
ui_dirty=$(git -C "$GACT_SRC" diff HEAD 2>/dev/null; git -C "$GACT_SRC" ls-files --others --exclude-standard -z 2>/dev/null | xargs -0 -r cat 2>/dev/null)
ui_id="$(head_of "$GACT_SRC") $(printf '%s' "$ui_dirty" | sha256sum | cut -c1-12) $brand_digest"
if [ "$DRY_RUN" = 1 ] || [ "$(cat "$BUILD/clio-web/BUILD-ID.txt" 2>/dev/null)" != "$ui_id" ]; then
  log "building the web UI with the CLIO brand"
  run bash -c "printf '{\"profile\":\"clio\",\"brandingRoot\":\"%s\"}\n' '$CLIO_SRC/branding' > '$GACT_SRC/brand.config.local.json'"
  run bash -c "cd '$GACT_SRC' && pnpm install --frozen-lockfile && pnpm build"
  run rm -rf "$BUILD/clio-web.new"
  run cp -a "$GACT_SRC/web/dist" "$BUILD/clio-web.new"
  run bash -c "echo '$ui_id' > '$BUILD/clio-web.new/BUILD-ID.txt' && rm -rf '$BUILD/clio-web' && mv '$BUILD/clio-web.new' '$BUILD/clio-web'"
fi
if [ "$DRY_RUN" != 1 ]; then
  [ -f "$BUILD/clio-web/index.html" ] || die "UI build produced no index.html"
  grep -qi 'clio' "$BUILD/clio-web/index.html" || log "WARNING: index.html does not mention CLIO; check brand.config.local.json"
fi
details+=("ui_build=\"$ui_id\"")

# --- attention connector: CLIO's pin + build fix (setuptools rejects the License classifier
# next to the SPDX expression; main e857fd0 still has it and lacks the TP head-count fix) ----------
C="$SRC/vllm-attn-connector"
if [ "$DRY_RUN" = 1 ] || ! [ -s "$CONNECTOR_SOURCE_FILE" ]; then
  if git -C "$C" apply --check "$KIT_DIR/connector-build-fix.patch" 2>/dev/null; then
    run git -C "$C" apply "$KIT_DIR/connector-build-fix.patch"
  elif ! git -C "$C" apply -R --check "$KIT_DIR/connector-build-fix.patch" 2>/dev/null && [ "$DRY_RUN" != 1 ]; then
    die "connector build fix neither applies nor is already applied at $(head_of "$C")"
  fi
  run rm -rf "$BUILD/connector"; run mkdir -p "$BUILD/connector"
  run bash -c "cd '$C' && uv build --wheel --out-dir '$BUILD/connector' ."
  run bash -c "w=\$(ls '$BUILD'/connector/vllm_attn_connector-*.whl) && echo \"file://\$w\" > '$CONNECTOR_SOURCE_FILE' && sha256sum \"\$w\" > '$BUILD/connector/SHA256'"
fi
details+=("connector_rev=\"$(head_of "$C")\""
          "connector_source=\"$(cat "$CONNECTOR_SOURCE_FILE" 2>/dev/null || true)\""
          "connector_wheel_sha256=\"$(cut -d' ' -f1 "$BUILD/connector/SHA256" 2>/dev/null || true)\"")

(export JOB_EVIDENCE="$STAGE"; receipt 01-stage ok "${details[@]}")
log "stage-up done; connector source: $(cat "$CONNECTOR_SOURCE_FILE" 2>/dev/null || echo '(dry run)')"
