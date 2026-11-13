#!/usr/bin/env bash
# CLIO installer (Linux / macOS).
#
#   install.sh                 the latest stable release
#   install.sh --dev           the latest release overall, pre-releases included
#   install.sh --source        source at `main`, with what main pins (submodules, uv.lock)
#   install.sh --dev --source  source at `develop`
#   install.sh --desktop       the desktop app, bundled with its agent (Apple Silicon macOS, Linux);
#                              --dev and --version pick the release as above
#   install.sh --desktop-package <file|https URL> [--desktop-sha256 <hex>]
#                              a pre-release desktop test build (for example a CI artifact)
#                              instead of a release; a URL needs --desktop-sha256
#   install.sh --reset-old-data  move data CLIO 0.9.5.1 or older wrote aside without asking
#                              (this release cannot open it; it is kept, never deleted)
#   --progress json            with --desktop: one JSON event per line on stdout (each
#                              step's begin/end and seconds, bytes, a summary)
#
# --source run from inside a clio-agent checkout (`./install/install.sh`) installs that
# checkout in place, on whatever branch it is; streamed (`curl ... | bash`) it clones.
# A release is installed from PyPI, constrained by that release tag's own uv.lock, on
# the Python that release declares (.python-version; 3.13 before the file existed). On
# Linux, a release that publishes its prebuilt runtime archive is installed from that
# instead, into the same environment; CLIO_INSTALL_FROM=pypi chooses PyPI.
#
# Pins for deployments and CI: --version <pypi version> (release) and --ref <git ref>
# (source). Older environment spellings still work: CLIO_VERSION=<v> is --version,
# CLIO_REF=<ref> is --source --ref <ref>.
#
# Honours environment overrides:
#   CLIO_PREFIX        install root         (default: <agent data>/app)
#   CLIO_BIN_DIR       launcher location    (default: $HOME/.local/bin)
#   CLIO_INSTALLER_REF pin launcher scripts (default: the installed release's tag)
#   CLIO_KIT_PACKAGE   local candidate wheel/base spec (default: released 2.12.7)
#   CLIO_GIT_PROTOCOL  https | ssh          (default: https; source mode only)
#   CLIO_DESKTOP_DIR   macOS app folder     (default: $HOME/Applications)
#   CLIO_INSTALL_PROGRESS  human | json     (default: human; as --progress)
#   CLIO_INSTALL_PROVIDER_SUPPORT  provider SDKs to install now (default: claude_code;
#                      empty: each installs on its first turn)
#
# NOTE: invoke via `bash` (not `sh`). On Debian/Ubuntu `sh` is dash,
# which doesn't support `set -o pipefail`. The one-liner in the README
# pipes to `bash` for this reason.

# Refuse to run under dash / POSIX-sh — we use `set -o pipefail` and a
# couple of bash-only constructs below. Piping into `sh` on Debian/
# Ubuntu (where /bin/sh is dash) trips this. Re-exec via curl isn't
# possible here because the script is being streamed from stdin, so
# just bail with the right next-step.
if [ -z "${BASH_VERSION:-}" ]; then
  echo "clio installer requires bash, not sh/dash." >&2
  echo "Re-run with bash, e.g.:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash" >&2
  exit 1
fi
set -euo pipefail
# CLIO_PORT names the server's API port, but clio-core reads the same name as its RPC
# port. The installer starts no server, and its migrate and document steps would
# otherwise put the clio-core daemon and client on the API port and stall until the
# daemon-start limit (180 s) before failing the migration.
unset CLIO_PORT
# The PATH this person's shell gave us, before the installer prepends its own bin dir.
CALLER_PATH="${PATH:-}"

usage() { sed -n '2,34p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

OPT_SOURCE=0
OPT_DEV=0
OPT_VERSION=""
OPT_REF=""
OPT_DESKTOP=0
OPT_DOWNLOAD_ONLY=""
OPT_DESKTOP_PACKAGE="${CLIO_DESKTOP_PACKAGE:-}"
OPT_DESKTOP_SHA256="${CLIO_DESKTOP_SHA256:-}"
OPT_RESET_OLD_DATA=0
OPT_PROGRESS="${CLIO_INSTALL_PROGRESS:-human}"
# Provider SDKs installed with the managed packages (empty: none); claude_code's is
# otherwise installed by the first Claude Code turn.
PROVIDER_SUPPORT="${CLIO_INSTALL_PROVIDER_SUPPORT-claude_code}"
while [ $# -gt 0 ]; do
  case "$1" in
    --source|-Source) OPT_SOURCE=1 ;;
    --dev|-Dev) OPT_DEV=1 ;;
    --version|-Version) OPT_VERSION="${2:?--version needs a value}"; shift ;;
    --version=*) OPT_VERSION="${1#*=}" ;;
    --ref|-Ref) OPT_REF="${2:?--ref needs a value}"; shift ;;
    --ref=*) OPT_REF="${1#*=}" ;;
    --desktop|-Desktop) OPT_DESKTOP=1 ;;
    --desktop-package) OPT_DESKTOP_PACKAGE="${2:?--desktop-package needs a file or URL}"; shift ;;
    --desktop-sha256) OPT_DESKTOP_SHA256="${2:?--desktop-sha256 needs a SHA-256}"; shift ;;
    --download-only|-DownloadOnly) OPT_DOWNLOAD_ONLY="${2:?--download-only needs a directory}"; shift ;;
    --reset-old-data|-ResetOldData) OPT_RESET_OLD_DATA=1 ;;
    --progress|-Progress) OPT_PROGRESS="${2:?--progress needs human or json}"; shift ;;
    --progress=*) OPT_PROGRESS="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "clio installer: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; RESET='\033[0m'
case "$OPT_PROGRESS" in
  human) PROGRESS_JSON=0 ;;
  json) PROGRESS_JSON=1 ;;
  *) echo "clio installer: --progress takes human or json" >&2; exit 2 ;;
esac
# With --progress json, stdout carries only JSON events; messages go to stderr.
say()  { if [ "${PROGRESS_JSON:-0}" = 1 ]; then printf "${GREEN}==>${RESET} %s\n" "$*" >&2; else printf "${GREEN}==>${RESET} %s\n" "$*"; fi; }
warn() { printf "${YELLOW}!! ${RESET} %s\n" "$*" >&2; }
die()  { printf "${RED}xx ${RESET} %s\n" "$*" >&2; FAILURE="$*"; exit 1; }
# die_with CODE MESSAGE: die, keeping the failed command's exit status.
die_with() { local code="$1"; shift; printf "${RED}xx ${RESET} %s\n" "$*" >&2; exit "$code"; }

have() { command -v "$1" >/dev/null 2>&1; }

# PyPI package spelling and GitHub tag spelling differ for beta releases.
release_tag() {
  local version="${1#v}"
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)b([0-9]+)\.post([0-9]+)$ ]]; then
    printf 'v%s-beta.%s.%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  elif [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?)b([0-9]+)$ ]]; then
    printf 'v%s-beta.%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[3]}"
  elif [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?(-beta\.[0-9]+(\.[0-9]+)?|(a|rc)[0-9]+)?$ ]]; then
    # Only betas have their own tag spelling; alphas and release candidates
    # are tagged as their package version (0.10.0a1 is v0.10.0a1).
    printf 'v%s' "$version"
  else
    die "no GitHub release tag for package version: $version"
  fi
}

# ---------- desktop app (--desktop) -----------------------------------
# The released desktop installer carries its own agent, so --desktop needs no Python,
# uv or git. Every OS runs the same steps, each timed and reported (install.ps1 too):
#   resolve   pick the release; read its checksum manifest and install plan
#   download  fetch the app and the plan's files (LibreOffice, pnpm) at the same time;
#             macOS unpacks the app archive while it arrives
#   verify    check every download against the release's SHA-256 manifest
#   install   put the app in place (macOS: check its signature first)
#   prepare   do the first launch's setup now, from the files already downloaded
# --download-only DIR stops after verify. --progress json prints one JSON event per
# line on stdout (step begin/end, download bytes, a final summary with each step's
# seconds), and the readable messages on stderr.
REPO_URL=https://github.com/iowarp/clio-agent
DESKTOP_SCRATCH=""
DESKTOP_STAGING=""
DESKTOP_DEST=""
# Where the .deb puts the app; the installer tests point it at a stand-in.
LINUX_DESKTOP="${CLIO_DESKTOP_BIN:-/usr/bin/clio-desktop}"
DESKTOP_JOBS=""
DESKTOP_MONITOR=""
DESKTOP_APP_FILE=""
DESKTOP_PLAN_BYTES=0
# The plan's app waits for a download in progress, so preparing need not wait for them.
DESKTOP_PLAN_WAITS=0
STEP_NAME=""
STEP_START=""
STEP_TIMES=""
RUN_START=""
RUN_BYTES=0

now() {
  local t
  t="$(date +%s.%N 2>/dev/null || true)"
  if [[ "$t" =~ ^[0-9]+\.[0-9][0-9][0-9] ]]; then printf '%s' "${BASH_REMATCH[0]}"; else printf '%s.000' "$(date +%s)"; fi
}
seconds_since() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.1f", b - a }'; }
json_text() { printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"; }
emit() { [ "$PROGRESS_JSON" = 0 ] || printf '%s\n' "$1"; }

# step NAME ends the running step and starts NAME; an empty NAME only ends it.
step() {
  if [ -n "$STEP_NAME" ]; then
    local seconds
    seconds="$(seconds_since "$STEP_START")"
    STEP_TIMES="$STEP_TIMES${STEP_TIMES:+,}\"$STEP_NAME\":$seconds"
    emit "{\"event\":\"step\",\"step\":\"$STEP_NAME\",\"state\":\"end\",\"seconds\":$seconds}"
  fi
  STEP_NAME="$1"
  STEP_START="$(now)"
  [ -n "$RUN_START" ] || RUN_START="$STEP_START"
  [ -z "$1" ] || emit "{\"event\":\"step\",\"step\":\"$1\",\"state\":\"begin\"}"
}

# finish_run true|false [ERROR]: the summary, once, after the last step or a failure.
finish_run() {
  [ -n "$RUN_START" ] || return 0
  local failed="$STEP_NAME" total
  if [ "$1" = true ]; then step ""; else STEP_NAME=""; fi
  total="$(seconds_since "$RUN_START")"
  RUN_START=""
  if [ "$1" = true ]; then
    emit "{\"event\":\"summary\",\"ok\":true,\"seconds\":{$STEP_TIMES},\"total\":$total,\"bytes\":$RUN_BYTES}"
    say "Done in $total s ($(printf '%s' "$STEP_TIMES" | tr -d '"' | sed 's/:/ /g; s/,/ s, /g') s)."
  else
    emit "{\"event\":\"summary\",\"ok\":false,\"step\":\"$failed\",\"error\":$(json_text "$2"),\"seconds\":{$STEP_TIMES},\"total\":$total,\"bytes\":$RUN_BYTES}"
  fi
}

desktop_cleanup() {
  local code=$? pid children
  # A job is a subshell; its curl would outlive it and keep writing into the scratch.
  # The subshell goes first: one that outlived its child would report it "Terminated".
  for pid in $DESKTOP_MONITOR $DESKTOP_JOBS; do
    children="$(pgrep -P "$pid" 2>/dev/null || true)"
    kill "$pid" 2>/dev/null || true
    # shellcheck disable=SC2086  # a list of PIDs
    [ -z "$children" ] || kill $children 2>/dev/null || true
  done
  if [ -n "$DESKTOP_SCRATCH" ]; then
    [ ! -d "$DESKTOP_SCRATCH/mount" ] || hdiutil detach "$DESKTOP_SCRATCH/mount" -quiet 2>/dev/null || true
    rm -rf "$DESKTOP_SCRATCH"
  fi
  [ -z "$DESKTOP_STAGING" ] || rm -rf "$DESKTOP_STAGING"
  [ "$code" = 0 ] || finish_run false "${FAILURE:-exit status $code}"
}

desktop_scratch() {
  DESKTOP_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/clio-desktop.XXXXXXXX")"
  mkdir "$DESKTOP_SCRATCH/downloads"
  trap desktop_cleanup EXIT
}

desktop_tag() {
  if [ -n "${OPT_VERSION:-${CLIO_VERSION:-}}" ]; then
    release_tag "${OPT_VERSION:-$CLIO_VERSION}"
  elif [ "$OPT_DEV" = 1 ]; then
    # The newest release, pre-releases included, never a draft (an authenticated
    # listing includes drafts; tag_name precedes draft in each release object).
    curl --proto '=https' --tlsv1.2 -fsSL "https://api.github.com/repos/iowarp/clio-agent/releases?per_page=30" \
      | awk '/"tag_name": *"v/ { tag=$0; sub(/.*"tag_name": *"/, "", tag); sub(/".*/, "", tag) }
             /"draft": *false/ && tag != "" && !found { print tag; found=1 }
             /"draft": *true/ { tag="" }'
  else
    local resolved
    resolved="$(curl --proto '=https' --tlsv1.2 -fsSL -o /dev/null -w '%{url_effective}' "$REPO_URL/releases/latest")"
    printf '%s' "${resolved##*/}"
  fi
}

# The asset's SHA-256 from a release manifest. GitHub turns spaces in asset names into
# dots and older manifests keep the original name; text and binary markers both occur.
manifest_sha() {
  awk -v name="$2" '
    substr($0,65,2) == "  " || substr($0,65,2) == " *" {
      file=substr($0,67); sub(/^CLIO Desktop/, "CLIO.Desktop", file)
      if (file == name) print substr($0,1,64)
    }' "$1"
}

file_sha() {
  if have sha256sum; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}
sha_stream() {
  if have sha256sum; then sha256sum | awk '{print $1}'; else shasum -a 256 | awk '{print $1}'; fi
}

# fetch_hashed URL FILE [curl options]: download URL to FILE, hashing the bytes as they
# arrive (FILE.sha), so verifying needs no second pass over the file. An interrupted
# download starts again once, from the beginning (curl cannot rewind into the hash).
fetch_hashed() {
  local url="$1" file="$2" attempt status
  shift 2
  for attempt in 1 2; do
    # tee empties FILE itself: it is never missing, so the app keeps waiting on a NAME.part.
    rm -f "$file.sha"
    curl --proto '=https' --tlsv1.2 -fsSL --speed-limit 1 --speed-time 60 "$@" "$url" \
      | tee "$file" | sha_stream > "$file.sha" && status=("${PIPESTATUS[@]}") || status=("${PIPESTATUS[@]}")
    [ "${status[0]}" != 0 ] || [ "${status[1]}" != 0 ] || [ "${status[2]}" != 0 ] || return 0
    [ "$attempt" = 2 ] || [ "${status[0]}" = 22 ] || continue
    break
  done
  rm -f "$file" "$file.sha"
  return 1
}

is_sha() { [[ "$1" =~ ^[0-9a-f]{64}$ ]]; }

file_bytes() { local n; n="$(wc -c < "$1" 2>/dev/null || echo 0)"; echo $(( n + 0 )); }

# Bytes downloaded so far, and expected in all (the app's size comes from its response).
downloaded_bytes() {
  local total=0 file
  # The streamed archive is removed once verified; app.bytes keeps its size.
  if [ -f "$DESKTOP_APP_FILE" ]; then total=$(file_bytes "$DESKTOP_APP_FILE")
  elif [ -f "$DESKTOP_SCRATCH/app.bytes" ]; then total=$(( $(cat "$DESKTOP_SCRATCH/app.bytes") + 0 )); fi
  for file in "$DESKTOP_SCRATCH"/downloads/*; do
    [ ! -f "$file" ] || total=$(( total + $(file_bytes "$file") ))
  done
  echo "$total"
}
expected_bytes() {
  local app=0
  [ ! -f "$DESKTOP_SCRATCH/app.headers" ] \
    || app="$(tr -d '\r' < "$DESKTOP_SCRATCH/app.headers" | awk 'tolower($1) == "content-length:" { n = $2 } END { print n + 0 }')"
  echo $(( app + DESKTOP_PLAN_BYTES ))
}
megabytes() { awk -v n="$1" 'BEGIN { printf "%.0f", n / 1000000 }'; }

# A line every 5 s (2 s as JSON) while the downloads run, so a long one never looks hung.
download_monitor() {
  local interval=5 last=0 bytes total
  [ "$PROGRESS_JSON" = 0 ] || interval=2
  # sleep must not hold the output open after the monitor is stopped.
  while sleep "$interval" </dev/null >/dev/null 2>&1; do
    bytes="$(downloaded_bytes)"; total="$(expected_bytes)"
    emit "{\"event\":\"progress\",\"step\":\"download\",\"bytes\":$bytes,\"total\":$total}"
    [ "$total" -gt 0 ] && total=" of $(megabytes "$total")" || total=""
    say "Downloaded $(megabytes "$bytes")$total MB ($(awk -v d=$(( bytes - last )) -v s="$interval" 'BEGIN { printf "%.1f", d / s / 1000000 }') MB/s)"
    last="$bytes"
    # Downloads can outlast the app's and run on through prepare; stop once all landed.
    total="$(expected_bytes)"
    [ ! -f "$DESKTOP_SCRATCH/app.headers" ] || [ "$bytes" -lt "$total" ] || break
  done
}
start_job() { "$@" & DESKTOP_JOBS="$DESKTOP_JOBS $!"; }
start_monitor() { download_monitor & DESKTOP_MONITOR=$!; }

# The release's install plan lists what the first launch would fetch (LibreOffice, pnpm)
# as "kind sha256 bytes name url"; only well-formed lines count. A "capability WORD" line
# says what the plan's app can do with them. plan_entries PLAN kind keeps the kind.
plan_entries() {
  awk -v kind="${2:-}" 'NF == 5 && length($2) == 64 && $2 ~ /^[0-9a-f]+$/ && $3 ~ /^[0-9]+$/ \
       && $4 ~ /^[A-Za-z0-9][A-Za-z0-9._+-]*$/ && $5 ~ /^https:\/\// {
         if (kind != "") print $1, $2, $3, $4, $5; else print $2, $3, $4, $5 }' "$1"
}
plan_capable() { [ -n "$1" ] && awk -v want="$2" '$1 == "capability" && $2 == want { found=1 } END { exit !found }' "$1"; }

# Root preparing for a sudo user cannot hand its private scratch files over.
plan_usable() { [ "$(id -u)" != 0 ] || [ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = root ]; }

fetch_plan() {  # TAG MANIFEST TRIPLE -> the plan file, or nothing
  local name="install-plan.$3.bundled.txt" expected
  expected="$(manifest_sha "$2" "$name")"
  is_sha "$expected" || return 0
  if curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$REPO_URL/releases/download/$1/$name" -o "$DESKTOP_SCRATCH/plan" \
     && [ "$(file_sha "$DESKTOP_SCRATCH/plan")" = "$expected" ]; then
    printf '%s' "$DESKTOP_SCRATCH/plan"
  else
    warn "could not read the release's install plan; the app downloads what it needs while preparing"
  fi
}

prefetch() {  # SHA NAME URL: a failure only means the app fetches it itself later
  local file="$DESKTOP_SCRATCH/downloads/$2"
  # NAME.part while it downloads, NAME once verified: the app waits on that (office.py).
  if fetch_hashed "$3" "$file.part" 2>/dev/null && [ "$(cat "$file.part.sha")" = "$1" ]; then
    mv "$file.part" "$file"
    rm -f "$file.part.sha"
  else
    rm -f "$file.part" "$file.part.sha"
    warn "could not fetch $2 ahead of time; the app downloads it while preparing"
  fi
}

prefetch_after() {  # PID SHA NAME URL: prefetch once job PID ends, or PREFETCH_WAIT_S at most
  local ticks=0
  while kill -0 "$1" 2>/dev/null && [ "$ticks" -lt $(( PREFETCH_WAIT_S * 5 )) ]; do
    sleep 0.2
    ticks=$(( ticks + 1 ))
  done
  shift
  prefetch "$@"
}

# start_prefetch PLAN [office-first]: with office-first, LibreOffice takes the line before
# the other large files. Every download shares one saturated line, so they all land at
# about the same time, and Office has the longest work after it lands (mount, copy, a
# first conversion), which then decides when the install ends. A file of another kind
# over PREFETCH_WAIT_BYTES (Chromium, whose unpack takes ~1 s) starts once LibreOffice
# has landed, or after PREFETCH_WAIT_S. It is an empty NAME.part until then, so the app
# waits for it rather than fetching it a second time; the app gives up on a NAME.part
# that has not grown in 60 s (office.py _INSTALLER_STALL_S), well beyond that wait.
PREFETCH_WAIT_BYTES=$(( 64 * 1024 * 1024 ))
PREFETCH_WAIT_S=40
start_prefetch() {
  local kind sha bytes name url names="" lead=""
  [ -n "$1" ] || return 0
  while read -r kind sha bytes name url; do
    DESKTOP_PLAN_BYTES=$(( DESKTOP_PLAN_BYTES + bytes ))
    names="$names${names:+, }$name"
    if [ -n "$lead" ] && [ "$kind" != office ] && [ "$bytes" -ge "$PREFETCH_WAIT_BYTES" ]; then
      : > "$DESKTOP_SCRATCH/downloads/$name.part"
      start_job prefetch_after "$lead" "$sha" "$name" "$url"
    else
      start_job prefetch "$sha" "$name" "$url"
      [ "${2:-}" != office-first ] || [ "$kind" != office ] || lead=$!
    fi
  done < <(plan_entries "$1" kind | awk '{ print ($1 == "office" ? 0 : 1), $0 }' | sort -s -n -k1,1 | cut -d' ' -f2-)
  [ -z "$names" ] || say "Fetching at the same time: $names"
}

finish_downloads() {
  local pid
  for pid in $DESKTOP_JOBS; do wait "$pid" 2>/dev/null || true; done
  [ -z "$DESKTOP_MONITOR" ] || { kill "$DESKTOP_MONITOR" 2>/dev/null; wait "$DESKTOP_MONITOR" 2>/dev/null; } || true
  DESKTOP_JOBS=""
  DESKTOP_MONITOR=""
  RUN_BYTES="$(downloaded_bytes)"
}

# A desktop test build that is not a release (a CI artifact): its checksum comes from the
# person who built it, and its version from the app itself. A file a browser downloaded is
# quarantined, which Gatekeeper refuses for a test build that is not notarized; the person
# chose this file explicitly, so the installed copy is not quarantined (as a curl download).
install_desktop_package() {
  local package="$OPT_DESKTOP_PACKAGE" sha os file app_dir version=""
  sha="$(printf '%s' "$OPT_DESKTOP_SHA256" | tr 'A-F' 'a-f')"
  [ -z "$sha" ] || is_sha "$sha" || die "--desktop-sha256 must be a SHA-256 (64 hex digits)"
  [ -z "$OPT_DOWNLOAD_ONLY" ] || die "--download-only selects a release; --desktop-package is already a file"
  os="$(uname -s)"
  desktop_scratch
  step resolve
  case "$os:${package%%\?*}" in
    Darwin:*.dmg|Darwin:*.aar|Darwin:*.tar.gz|Linux:*.deb|Linux:*.rpm) ;;
    *) die "--desktop-package on $os takes a $( [ "$os" = Darwin ] && echo .dmg, .app.aar or .app.tar.gz || echo .deb or .rpm )" ;;
  esac
  [ "$os" != Darwin ] || mac_destination
  case "$package" in
    https://*)
      [ -n "$sha" ] || die "a --desktop-package URL needs --desktop-sha256"
      have curl || die "curl is required"
      file="${package%%\?*}"; file="$DESKTOP_SCRATCH/${file##*/}"
      step download
      say "Downloading the CLIO Desktop test build ($package)"
      DESKTOP_APP_FILE="$file"
      start_monitor
      curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -D "$DESKTOP_SCRATCH/app.headers" "$package" -o "$file" \
        || die "could not download $package"
      finish_downloads ;;
    *://*) die "--desktop-package must be a local file or an https URL" ;;
    *) [ -f "$package" ] || die "no such desktop package: $package"; file="$package" ;;
  esac
  step verify
  if [ -n "$sha" ]; then
    [ "$(file_sha "$file")" = "$sha" ] || die "desktop package checksum mismatch; nothing installed"
  else
    warn "no --desktop-sha256 given: installing $file unverified"
  fi
  step install
  case "$file" in
    *.dmg) install_desktop_mac "$file" --noqtn ;;
    *.aar|*.tar.gz)
      mac_staging
      say "Unpacking CLIO Desktop into $DESKTOP_DEST..."
      mac_unpack "$file" "$DESKTOP_STAGING" < "$file" || die "could not unpack $file; nothing installed"
      mac_install_staged ;;
    *) install_desktop_linux "$file" ;;
  esac
  step prepare
  if [ "$os" = Darwin ]; then
    app_dir="$DESKTOP_DEST/CLIO Desktop.app"
    version="$(plutil -extract CFBundleShortVersionString raw -o - "$app_dir/Contents/Info.plist" 2>/dev/null || true)"
    say "Installed the CLIO Desktop test build ${version:+$version }(not a release)"
    prepare_desktop "" "$app_dir/Contents/MacOS/clio-desktop"
  else
    prepare_desktop "" "$LINUX_DESKTOP"
  fi
  finish_run true
}

# The macOS release asset: the app as an Apple Archive, unpacked while it downloads;
# releases before it have the updater's .app.tar.gz, which streams the same way; the
# disk image is the fallback, and what --download-only keeps. CLIO_DESKTOP_FORMAT
# (aar, tar.gz or dmg) picks one, for comparisons.
mac_release_asset() {  # MANIFEST VERSION -> the asset name
  local stem="CLIO.Desktop-aarch64-apple-darwin-bundled.app" format dmg="CLIO.Desktop_${2}_aarch64-bundled.dmg"
  if [ -n "$OPT_DOWNLOAD_ONLY" ]; then printf '%s' "$dmg"; return; fi
  for format in ${CLIO_DESKTOP_FORMAT:-aar tar.gz dmg}; do
    case "$format" in
      aar|tar.gz) [ -z "$(manifest_sha "$1" "$stem.$format")" ] || { printf '%s' "$stem.$format"; return; } ;;
      dmg) printf '%s' "$dmg"; return ;;
      *) die "CLIO_DESKTOP_FORMAT must be aar, tar.gz or dmg" ;;
    esac
  done
  die "this release has no $CLIO_DESKTOP_FORMAT desktop asset"
}

install_desktop() {
  if [ -n "$OPT_DESKTOP_PACKAGE" ]; then install_desktop_package; return; fi
  [ "$OPT_SOURCE" = 0 ] || die "--desktop installs a released app; --source builds the agent only (build the app from gact-tui)"
  have curl || die "curl is required"
  local os cpu tag version asset triple expected url plan="" fetch
  os="$(uname -s)"; cpu="$(uname -m)"
  desktop_scratch
  step resolve
  case "$os" in
    Darwin)
      [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ] || die "CLIO Desktop requires macOS 15 or newer"
      # uname can describe the Rosetta shell rather than the physical machine.
      [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ] || [ "$cpu" = arm64 ] \
        || die "CLIO Desktop is not available for Intel Macs yet; install CLIO without --desktop instead"
      triple=aarch64-apple-darwin ;;
    Linux)
      case "$cpu" in
        x86_64|amd64) triple=x86_64-unknown-linux-gnu ;;
        aarch64|arm64) triple=aarch64-unknown-linux-gnu ;;
        *) die "unsupported processor: $cpu" ;;
      esac
      [ -n "$(linux_package_format)" ] || die "CLIO Desktop is published as .deb, .rpm and Arch Linux packages, and this Linux has none of dpkg, rpm or pacman. Install CLIO without --desktop and open it in your browser with: clio web" ;;
    *) die "unsupported OS for --desktop: $os (use install.ps1 -Desktop on Windows)" ;;
  esac
  tag="$(desktop_tag)"
  [ -n "$tag" ] || die "could not find a CLIO release on GitHub"
  version="${tag#v}"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+|-beta\.[0-9]+(\.[0-9]+)?|(a|rc)[0-9]+)?$ ]] \
    || die "invalid release version: $tag"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$REPO_URL/releases/download/$tag/SHA256SUMS.$triple.bundled.txt" -o "$DESKTOP_SCRATCH/checksums" \
    || die "could not download the release checksums (SHA256SUMS.$triple.bundled.txt)"
  case "$triple" in
    *-apple-darwin) asset="$(mac_release_asset "$DESKTOP_SCRATCH/checksums" "$version")" ;;
    *-linux-gnu) asset="$(linux_package_asset "$version" "${triple%%-*}")" ;;
  esac
  expected="$(manifest_sha "$DESKTOP_SCRATCH/checksums" "$asset" | tr 'A-F' 'a-f')"
  if ! is_sha "$expected" && [ "$(linux_package_format)" = pacman ]; then
    die "CLIO Desktop $tag has no Arch Linux package (the first is in the release after 0.9.6.1). Install a newer release, or install CLIO without --desktop and open it in your browser with: clio web"
  fi
  is_sha "$expected" || die "release checksum missing or ambiguous for $asset; nothing installed"
  if [ -z "$OPT_DOWNLOAD_ONLY" ]; then
    [ "$os" != Darwin ] || mac_destination
    ! plan_usable || plan="$(fetch_plan "$tag" "$DESKTOP_SCRATCH/checksums" "$triple")"
    ! plan_capable "$plan" waits-for-partial-downloads || DESKTOP_PLAN_WAITS=1
    # The deb and rpm carry their runtime; the plan's runtime archive is the one-liner's.
    if [ -n "$plan" ]; then
      awk '$1 != "runtime"' "$plan" > "$DESKTOP_SCRATCH/plan.desktop"
      plan="$DESKTOP_SCRATCH/plan.desktop"
    fi
  fi
  url="$REPO_URL/releases/download/$tag/$asset"
  step download
  [ "$os" != Linux ] || [ -n "$OPT_DOWNLOAD_ONLY" ] || start_webkit_check
  start_prefetch "$plan"
  case "$asset" in
    *.aar|*.tar.gz)
      say "Downloading CLIO Desktop $tag ($asset), unpacking it into $DESKTOP_DEST as it arrives"
      DESKTOP_APP_FILE="$DESKTOP_SCRATCH/app.part"
      start_monitor
      mac_stream "$url" ;;
    *)
      say "Downloading CLIO Desktop $tag ($asset)"
      DESKTOP_APP_FILE="$DESKTOP_SCRATCH/$asset"
      start_monitor
      fetch_hashed "$url" "$DESKTOP_APP_FILE" -D "$DESKTOP_SCRATCH/app.headers" &
      fetch=$!
      DESKTOP_JOBS="$DESKTOP_JOBS $fetch"
      # A system that cannot install the package stops the download as soon as it says so.
      while kill -0 "$fetch" 2>/dev/null; do
        [ "$(webkit_check_result)" != missing ] || die "$WEBKIT_MISSING"
        sleep 0.2
      done
      wait "$fetch" || die "could not download $asset from release $tag" ;;
  esac
  [ -z "$WEBKIT_CHECK" ] || { wait "$WEBKIT_CHECK" 2>/dev/null || true; }
  [ "$(webkit_check_result)" != missing ] || die "$WEBKIT_MISSING"
  # The app is what the next steps need. Its first-launch files keep downloading through
  # them when the app waits for them; an older app would fetch them a second time.
  [ "$DESKTOP_PLAN_WAITS" = 1 ] || finish_downloads
  step verify
  case "$asset" in
    *.aar|*.tar.gz) [ "$(cat "$DESKTOP_SCRATCH/app.sha")" = "$expected" ] || die "download checksum mismatch; nothing installed"
      if [ -z "$OPT_DOWNLOAD_ONLY" ]; then
        file_bytes "$DESKTOP_APP_FILE" > "$DESKTOP_SCRATCH/app.bytes"
        rm -f "$DESKTOP_APP_FILE"
      fi ;;
    *) [ "$(cat "$DESKTOP_APP_FILE.sha")" = "$expected" ] || die "download checksum mismatch; nothing installed" ;;
  esac
  if [ -n "$OPT_DOWNLOAD_ONLY" ]; then
    mkdir -p "$OPT_DOWNLOAD_ONLY"
    cp "$DESKTOP_APP_FILE" "$OPT_DOWNLOAD_ONLY/$asset"
    say "Verified download: $OPT_DOWNLOAD_ONLY/$asset"
    finish_run true
    return 0
  fi
  step install
  case "$asset" in
    *.aar|*.tar.gz) mac_install_staged ;;
    *.dmg) install_desktop_mac "$DESKTOP_APP_FILE" ;;
    *) install_desktop_linux "$DESKTOP_APP_FILE" ;;
  esac
  step prepare
  case "$os" in
    Darwin) prepare_desktop "$tag" "$DESKTOP_DEST/CLIO Desktop.app/Contents/MacOS/clio-desktop" "$DESKTOP_SCRATCH/downloads" ;;
    Linux) prepare_desktop "$tag" "$LINUX_DESKTOP" "$DESKTOP_SCRATCH/downloads" ;;
  esac
  finish_downloads
  finish_run true
}

# Releases before 0.9.5 have no --prepare-runtime on macOS and Linux; there an unknown
# flag would open the app instead.
desktop_prepares() {
  local version="${1#v}" base
  base="${version%%-*}"
  [ "$base" = 0.9.5 ] && [ "$base" != "$version" ] && return 1
  [ "$(printf '%s\n%s\n' 0.9.5 "$base" | sort -V | head -n 1)" = 0.9.5 ]
}

# What a first launch would do before the agent starts (its tool packages, Office),
# done now, as the person who will open the app, as the Windows installer does. DOWNLOADS
# holds the release plan's files fetched with the app; the app uses those whose SHA-256
# matches its own pins instead of downloading them again. A failure fails the install, as
# the Windows setup does: none of this work is left to the first launch.
# An empty tag is a current test build, which can.
# No bytecode is written while preparing: releases up to 0.9.5.1 compile .pyc files into
# the signed bundle (#1979), which breaks the signature the install just checked.
prepare_desktop() {
  local tag="$1" desktop="$2" downloads="${3:-}" out=1
  [ -z "$tag" ] || desktop_prepares "$tag" || return 0
  [ -x "$desktop" ] || die "$desktop not found after installing; the install is incomplete. Run this installer again."
  [ "${PROGRESS_JSON:-0}" = 0 ] || out=2
  say "Preparing the desktop's agent and its tools"
  local run=(env PYTHONDONTWRITEBYTECODE=1 "$desktop" --prepare-runtime)
  if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    run=(sudo -u "$SUDO_USER" -H "${run[@]}")
  elif [ -n "$downloads" ]; then
    run=(env PYTHONDONTWRITEBYTECODE=1 "CLIO_INSTALL_DOWNLOADS=$downloads" "$desktop" --prepare-runtime)
  fi
  # The install finishes this work or fails: it never moves to the first launch.
  "${run[@]}" >&"$out" || die "preparing the desktop's agent and its tools failed (the cause is above), so the install is incomplete. Fix it and run this installer again, or run: \"$desktop\" --prepare-runtime"
  say "The desktop is ready to open."
}

# A mirror+file: source names a file of mirrors; the first one serves the download.
apt_mirror_uri() {  # URI -> an http(s) URI, or nothing
  local uri="$1" list base
  case "$uri" in
    http://*|https://*) printf '%s' "$uri"; return ;;
    mirror+file:/*) ;;
    *) return 0 ;;
  esac
  list="${uri#mirror+file:}"
  while [ ! -f "$list" ] && [ "$list" != "${list%/*}" ]; do list="${list%/*}"; done
  [ -f "$list" ] || return 0
  base="$(awk '$1 ~ /^https?:\/\// { print $1; exit }' "$list")"
  [ -z "$base" ] || printf '%s/%s' "${base%/}" "${uri#mirror+file:"$list"/}"
}

# apt fetches the desktop's system libraries (WebKitGTK, GStreamer: ~70 packages on a
# system without them) one at a time from one mirror, where one slow response stalls the
# rest. They are fetched 8 at a time into a private archive directory instead, a stalled
# transfer restarting; apt then checks each against its signed index (size and hash)
# and fetches any that are missing or wrong itself. Behind an apt proxy apt does it all.
apt_prefetch() {  # SUDO PACKAGE DIR
  local sudo="$1" package="$2" dir="$3" uri name size rest url count=0 bytes=0
  mkdir -p "$dir/partial"
  apt-config dump > "$dir/config" 2>/dev/null || true
  ! grep -qiE '^Acquire::[a-z]+::Proxy "[^"]' "$dir/config" || return 1
  $sudo apt-get install -y -qq --print-uris "$package" 2>/dev/null > "$dir/uris" || return 1
  while read -r uri name size rest; do
    url="$(apt_mirror_uri "${uri//\'/}")"
    [ -n "$url" ] && [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.+~%_-]*\.deb$ ]] || continue
    printf '%s %s\n' "$dir/$name" "$url"
    size="${size//[^0-9]/}"; count=$(( count + 1 )); bytes=$(( bytes + ${size:-0} ))
  done < "$dir/uris" > "$dir/fetch"
  [ "$count" -gt 0 ] || return 0
  say "Fetching $count system packages CLIO Desktop needs ($(megabytes "$bytes") MB), 8 at a time"
  # shellcheck disable=SC2016
  xargs -P 8 -n 2 sh -c 'curl -fsSL --connect-timeout 10 --speed-limit 50000 --speed-time 10 --retry 3 \
      -o "$0.part" "$1" && mv "$0.part" "$0" || rm -f "$0.part"; exit 0' < "$dir/fetch"
}

# The desktop package format for this Linux: the one its own package manager installs
# with dependencies (apt: deb; dnf or zypper: rpm; pacman: Arch), else one dpkg or rpm
# installs alone. An Arch system can carry rpm from its repositories; it gets its own
# package, not an rpm whose dependencies nothing would resolve.
linux_package_format() {
  if have apt-get; then echo deb
  elif have dnf || have zypper; then echo rpm
  elif have pacman; then echo pacman
  elif have dpkg; then echo deb
  elif have rpm; then echo rpm
  fi
}

linux_package_asset() {  # VERSION ARCH (x86_64, aarch64)
  case "$(linux_package_format)-$2" in
    deb-x86_64) printf 'CLIO.Desktop_%s_amd64-bundled.deb' "$1" ;;
    deb-aarch64) printf 'CLIO.Desktop_%s_arm64-bundled.deb' "$1" ;;
    rpm-*) printf 'CLIO.Desktop-%s-1.%s-bundled.rpm' "$1" "$2" ;;
    pacman-*) printf 'CLIO.Desktop-%s-1-%s-bundled.pkg.tar.zst' "$1" "$2" ;;
  esac
}

# LINUX_SUDO: what runs a package manager as root: nothing for root, else sudo.
LINUX_SUDO=""
linux_sudo() {
  LINUX_SUDO=""
  [ "$(id -u)" = 0 ] && return 0
  have sudo || die "installing CLIO Desktop needs root; run as root or install sudo"
  LINUX_SUDO=sudo
}

# 0 when this system's repositories have no WebKitGTK 4.1, which the Tauri 2 desktop
# links (RHEL, Rocky and Alma 9 ship only the 4.0 API): such a system would otherwise
# learn it from dnf after downloading ~860 MB. The query runs as root so the metadata it
# fetches is the cache the install reads. An answer that cannot be had (no network, apt
# lists never fetched) is not "missing": the install step reports what it finds.
desktop_webkit_missing() {  # SUDO
  local lib='libwebkit2gtk-4.1.so.0()(64bit)' found rc=0
  if have dnf || have zypper; then
    ! rpm -q --whatprovides "$lib" >/dev/null 2>&1 || return 1
  fi
  if have dnf; then
    found="$($1 dnf -q repoquery --whatprovides "$lib" 2>/dev/null)" || return 1
    [ -z "$found" ]
  elif have zypper; then
    $1 zypper --non-interactive --quiet search --provides --match-exact "$lib" >/dev/null 2>&1 || rc=$?
    [ "$rc" = 104 ]  # zypper: no package matched
  elif have pacman; then
    ! pacman -Qq webkit2gtk-4.1 >/dev/null 2>&1 || return 1
    pacman -Sp --print-format '%n' webkit2gtk-4.1 >/dev/null 2>&1 && return 1
    # Not in the sync databases, which may simply never have been fetched: unknown.
    compgen -G '/var/lib/pacman/sync/*.db' >/dev/null
  elif have apt-cache; then
    ! dpkg -s libwebkit2gtk-4.1-0 >/dev/null 2>&1 || return 1
    compgen -G '/var/lib/apt/lists/*_Packages*' >/dev/null || return 1
    ! apt-cache show libwebkit2gtk-4.1-0 >/dev/null 2>&1
  else
    return 1
  fi
}

# The WebKit check runs beside the download: dnf and zypper spend 9-40 s fetching the
# repository metadata the install needs anyway, which the download then hides. The
# install reads it from their cache. sudo asks for its password first, in the
# foreground, where a prompt can be answered.
WEBKIT_CHECK=""
WEBKIT_MISSING="this system's package repositories provide no WebKitGTK 4.1 (libwebkit2gtk-4.1), which CLIO Desktop needs (RHEL, Rocky and Alma 9 ship only an older WebKitGTK). Install CLIO without --desktop and open it in your browser with: clio web"
start_webkit_check() {
  linux_sudo
  # A command, not `sudo -v`: -v needs every matching sudoers rule to be passwordless
  # (openSUSE's default `ALL ALL=(ALL) ALL` with targetpw), so it asks where they never do.
  [ -z "$LINUX_SUDO" ] || sudo true || die "installing CLIO Desktop needs root, and sudo refused"
  {
    if desktop_webkit_missing "$LINUX_SUDO"; then echo missing; else echo found; fi > "$DESKTOP_SCRATCH/webkit.part"
    mv "$DESKTOP_SCRATCH/webkit.part" "$DESKTOP_SCRATCH/webkit"
  } </dev/null >/dev/null 2>&1 &
  WEBKIT_CHECK=$!
  DESKTOP_JOBS="$DESKTOP_JOBS $WEBKIT_CHECK"
}
webkit_check_result() { cat "$DESKTOP_SCRATCH/webkit" 2>/dev/null || true; }

install_desktop_linux() {
  local package="$1" sudo out=1 archives=()
  [ "$PROGRESS_JSON" = 0 ] || out=2
  linux_sudo
  sudo="$LINUX_SUDO"
  case "$package" in
    *.deb)
      if have apt-get; then
        ! apt_prefetch "$sudo" "$package" "$DESKTOP_SCRATCH/apt" || archives=(-o "Dir::Cache::Archives=$DESKTOP_SCRATCH/apt/")
        $sudo apt-get install -y ${archives[@]+"${archives[@]}"} "$package" >&"$out" || die "apt-get could not install $package"
      else $sudo dpkg -i "$package" >&"$out" || die "dpkg could not install $package (missing dependencies: install them, then retry)"; fi ;;
    *.rpm)
      # rpm alone resolves no dependencies (WebKitGTK, GTK, zstd): dnf or zypper fetch them.
      if have dnf; then $sudo dnf install -y "$package" >&"$out" || die "dnf could not install $package"
      elif have zypper; then
        # The package is not signed with a key zypper knows; its SHA-256 was checked above.
        $sudo zypper --non-interactive install --allow-unsigned-rpm "$package" >&"$out" \
          || die "zypper could not install $package"
      else $sudo rpm -U --replacepkgs "$package" >&"$out" || die "rpm could not install $package (missing dependencies: install them, then retry)"; fi ;;
    *.pkg.tar.zst)
      # Unsigned, as the deb and rpm are: its SHA-256 was checked above, and pacman checks
      # each file against the package's own list. A re-run reinstalls it (a repair).
      $sudo pacman -U --noconfirm "$package" >&"$out" || die "pacman could not install $package" ;;
  esac
  linux_runtime_ready "$sudo" "$out"
  say "CLIO Desktop installed. Open it from your applications menu, or run clio-desktop."
}

# A bundled package unpacks its runtime archive in its postinst. Run that step again:
# rpm erases the files of the package it upgrades after the new %post, which removes
# what the first upgrade from an exploded-runtime rpm unpacked. It does nothing when the
# runtime already matches the archive.
linux_runtime_ready() {  # SUDO OUT
  local root="/usr/lib/CLIO Desktop"
  [ -f "$root/gact-runtime.pack.json" ] || return 0
  [ -f "$root/gact-runtime.unpack.sh" ] || die "the CLIO Desktop package has no runtime unpacker"
  $1 sh "$root/gact-runtime.unpack.sh" >&"$2" \
    || die "the CLIO runtime could not be unpacked; reinstall the clio-desktop package"
  [ -f "$root/gact-runtime/runtime.json" ] || die "the CLIO runtime is missing after install"
}

# A CLIO Desktop running from DESTINATION would be replaced under it. One running from
# elsewhere (another copy) is left alone; one whose path cannot be read counts as here.
desktop_running_in() {
  local pids pid path real=""
  pids="$(pgrep -x clio-desktop 2>/dev/null)" || return 1
  [ -n "$pids" ] || return 0
  [ ! -d "$1" ] || real="$(cd -P "$1" && pwd)"
  for pid in $pids; do
    path="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
    case "$path" in
      "$1"/*) return 0 ;;
      /*) [ -z "$real" ] || case "$path" in "$real"/*) return 0 ;; esac ;;
      *) return 0 ;;
    esac
  done
  return 1
}

mac_destination() {
  DESKTOP_DEST="${CLIO_DESKTOP_DIR:-$HOME/Applications}"
  case "$DESKTOP_DEST" in /*) ;; *) die "CLIO_DESKTOP_DIR must be absolute" ;; esac
  ! desktop_running_in "$DESKTOP_DEST/CLIO Desktop.app" || die "Quit CLIO Desktop before installing."
}

# The new app is assembled beside the old one, on the same volume, so it moves in at once.
mac_staging() {
  mkdir -p "$DESKTOP_DEST"
  [ -z "$DESKTOP_STAGING" ] || rm -rf "$DESKTOP_STAGING"
  DESKTOP_STAGING="$(mktemp -d "$DESKTOP_DEST/.clio-install.XXXXXXXX")"
}

# mac_unpack NAME DIR: unpack the app archive on stdin. Both tools refuse absolute paths,
# `..` and writing through links; nothing they write is used before its checksum and the
# app's signature pass.
mac_unpack() {
  case "$1" in
    *.aar) aa extract -d "$2" ;;
    *) tar -xzf - -C "$2" ;;
  esac
}

# Unpack the app as it downloads; the same bytes are hashed for the verify step and
# counted for progress. An interrupted download starts again once, from the beginning.
mac_stream() {
  local url="$1" attempt hasher
  for attempt in 1 2; do
    mac_staging
    rm -f "$DESKTOP_SCRATCH/app.part" "$DESKTOP_SCRATCH/app.sha" "$DESKTOP_SCRATCH/app.fifo"
    mkfifo "$DESKTOP_SCRATCH/app.fifo"
    { shasum -a 256 | awk '{ print $1 }' > "$DESKTOP_SCRATCH/app.sha"; } < "$DESKTOP_SCRATCH/app.fifo" &
    hasher=$!
    if curl --proto '=https' --tlsv1.2 -fsSL --speed-limit 1 --speed-time 60 -D "$DESKTOP_SCRATCH/app.headers" "$url" \
         | tee "$DESKTOP_SCRATCH/app.fifo" "$DESKTOP_SCRATCH/app.part" \
         | mac_unpack "$url" "$DESKTOP_STAGING"; then
      wait "$hasher"
      return 0
    fi
    wait "$hasher" 2>/dev/null || true
    [ "$attempt" = 2 ] || warn "the download was interrupted; starting it again"
  done
  die "could not download or unpack $url; nothing installed"
}

# Check the staged app's signature once, then swap it in, keeping the previous app. The
# check finishes before anything runs from the bundle: preparing runs the app, and a
# release whose runtime writes into its own bundle (#1979) would fail a check still
# reading it, which then removed a working app.
mac_install_staged() {
  local app="$DESKTOP_STAGING/CLIO Desktop.app" backup=""
  [ -d "$app" ] || die "the download does not contain CLIO Desktop.app; nothing installed"
  say "Checking the app's signature..."
  codesign --verify --deep --strict "$app" || die "the app's signature is invalid or absent; nothing installed"
  say "Signature valid."
  if [ -e "$DESKTOP_DEST/CLIO Desktop.app" ]; then
    backup="$DESKTOP_DEST/CLIO Desktop.previous.$(date +%Y%m%d%H%M%S).$$.app"
    mv "$DESKTOP_DEST/CLIO Desktop.app" "$backup"
  fi
  if ! mv "$app" "$DESKTOP_DEST/CLIO Desktop.app"; then
    [ -z "$backup" ] || mv "$backup" "$DESKTOP_DEST/CLIO Desktop.app"
    die "could not install the app; the previous app was restored"
  fi
  rm -rf "$DESKTOP_STAGING"
  DESKTOP_STAGING=""
  say "Installed: $DESKTOP_DEST/CLIO Desktop.app"
  [ -z "$backup" ] || say "Previous app kept: $backup"
  # curl does not quarantine what it downloads, so Gatekeeper does not prompt for this copy.
  say "Open it from $DESKTOP_DEST in Finder, or run: open \"$DESKTOP_DEST/CLIO Desktop.app\""
}

# install_desktop_mac IMAGE [--noqtn]: copy the app out of a disk image (--noqtn leaves
# quarantine off the copy). The image's SHA-256 is already checked, so attach skips its
# own checksum pass (-noverify).
install_desktop_mac() {
  local image="$1" ditto_flags=("${@:2}")
  mkdir "$DESKTOP_SCRATCH/mount"
  say "Opening the disk image"
  hdiutil attach "$image" -readonly -nobrowse -noverify -mountpoint "$DESKTOP_SCRATCH/mount" -quiet \
    || die "could not open the disk image $image"
  [ -d "$DESKTOP_SCRATCH/mount/CLIO Desktop.app" ] || die "the disk image does not contain CLIO Desktop.app"
  mac_staging
  say "Copying CLIO Desktop into $DESKTOP_DEST (about 1.7 GB; this takes a few minutes)..."
  ditto ${ditto_flags[@]+"${ditto_flags[@]}"} "$DESKTOP_SCRATCH/mount/CLIO Desktop.app" "$DESKTOP_STAGING/CLIO Desktop.app"
  mac_install_staged
}

if [ "$OPT_DESKTOP" = 1 ] || [ -n "$OPT_DESKTOP_PACKAGE" ]; then
  install_desktop
  exit 0
fi
[ -z "$OPT_DOWNLOAD_ONLY" ] || die "--download-only goes with --desktop"

# ---------- defaults ---------------------------------------------------
if [[ "$(uname -s)" = Darwin* ]]; then
  agent_data_default="$HOME/Library/Application Support/clio-agent/data"
else
  xdg_data="${XDG_DATA_HOME:-}"
  case "$xdg_data" in /*) ;; *) xdg_data="$HOME/.local/share" ;; esac
  agent_data_default="$xdg_data/clio-agent"
fi
agent_data="${CLIO_AGENT_DATA_DIR:-${CLIO_AGENT_HOME:+$CLIO_AGENT_HOME/data}}"
agent_data="${agent_data:-${CLIO_USER_DIR:+$CLIO_USER_DIR/data}}"
agent_data="${agent_data:-$agent_data_default}"
case "$agent_data" in /*) ;; *) echo "Agent data root must be absolute" >&2; exit 2 ;; esac
PREFIX="${CLIO_PREFIX:-$agent_data/app}"
# An existing legacy install remains addressable until explicit migration.
if [[ -z "${CLIO_PREFIX:-}" && -z "${CLIO_AGENT_HOME:-}${CLIO_AGENT_DATA_DIR:-}${CLIO_USER_DIR:-}" && ! -d "$PREFIX/clio-agent/.venv" && -d "$HOME/.local/share/clio/clio-agent/.venv" ]]; then
  PREFIX="$HOME/.local/share/clio"
fi
BIN_DIR="${CLIO_BIN_DIR:-$HOME/.local/bin}"
OPT_VERSION="${OPT_VERSION:-${CLIO_VERSION:-}}"
if [ -n "${CLIO_REF:-}" ] && [ -z "$OPT_REF" ]; then
  OPT_SOURCE=1
  OPT_REF="$CLIO_REF"
fi
[ "$OPT_SOURCE" = 1 ] && [ -n "$OPT_VERSION" ] && die "--version pins a release; use --ref <tag or commit> with --source"
[ "$OPT_SOURCE" = 0 ] && [ -n "$OPT_REF" ] && die "--ref pins source; add --source"
CLIO_INSTALLER_REF="${CLIO_INSTALLER_REF:-}"
CLIO_KIT_PACKAGE="${CLIO_KIT_PACKAGE:-}"
CLIO_GIT_PROTOCOL="${CLIO_GIT_PROTOCOL:-https}"

case "$CLIO_GIT_PROTOCOL" in
  https)
    CLIO_REPO="https://github.com/iowarp/clio-agent.git"
    ;;
  ssh)
    CLIO_REPO="git@github.com:iowarp/clio-agent.git"
    ;;
  *)
    die "CLIO_GIT_PROTOCOL must be 'https' or 'ssh' (got: $CLIO_GIT_PROTOCOL)"
    ;;
esac

# ---------- platform detection ----------------------------------------
case "$(uname -s)" in
  Linux*)  OS=linux  ;;
  Darwin*) OS=darwin ;;
  *) die "unsupported OS: $(uname -s) (use install.ps1 on Windows)" ;;
esac
# uv's interpreter discovery runs /usr/bin/python3, which on a Mac without the
# Command Line Tools is a stub that opens their install dialog. Use uv's own Pythons.
if [ "$OS" = darwin ]; then
  export UV_MANAGED_PYTHON="${UV_MANAGED_PYTHON:-1}"
fi
case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) die "unsupported arch: $(uname -m)" ;;
esac

# ---------- prerequisite checks ---------------------------------------
have curl || die "curl is required"

# A fresh Mac's /usr/bin developer tools (git, install_name_tool, python3, ...) are
# placeholders: running one opens the "Install Command Line Developer Tools" dialog.
# They work once Xcode or the Command Line Tools provide a developer directory.
macos_developer_tools() {
  local developer link
  link="$(readlink /var/db/xcode_select_link 2>/dev/null || true)"
  for developer in "${DEVELOPER_DIR:-}" "$link" /Applications/Xcode.app/Contents/Developer \
      /Library/Developer/CommandLineTools; do
    [ -n "$developer" ] && [ -d "$developer/usr/bin" ] && return 0
  done
  return 1
}

# uv runs install_name_tool on each Python it installs (to patch libpython's install
# name) and only warns when that fails. Without the developer tools a stand-in that
# fails comes first on PATH, so installing a Python never opens the dialog.
tool_shim=""
if [ "$OS" = darwin ] && ! macos_developer_tools; then
  tool_shim="$(mktemp -d "${TMPDIR:-/tmp}/clio-tools.XXXXXXXX")"
  printf '#!/bin/sh\necho "clio: install_name_tool needs the Command Line Tools; the library is left as it is" >&2\nexit 1\n' \
    > "$tool_shim/install_name_tool"
  chmod +x "$tool_shim/install_name_tool"
  export PATH="$tool_shim:$PATH"
  trap 'rm -rf "$tool_shim"' EXIT  # on_exit, below, takes over
fi

# What this installer added outside its own folders, one "kind value" line each, so
# uninstall.sh removes exactly that: "file PATH" (uv and uvx, only when installed here)
# and "uv-tool NAME" (clio-kit). A user's own uv is never listed.
INSTALLED_TOOLS="$PREFIX/installed-tools"
# An install from before this record has its clio-kit from that installer.
UNRECORDED_INSTALL=0
[ -e "$INSTALLED_TOOLS" ] || [ ! -d "$PREFIX/clio-agent" ] || UNRECORDED_INSTALL=1
record_installed() {  # KIND VALUE
  grep -qxF "$1 $2" "$INSTALLED_TOOLS" 2>/dev/null || printf '%s %s\n' "$1" "$2" >> "$INSTALLED_TOOLS"
}

# A fresh Mac's /usr/bin/git is a placeholder (above). Decided from the filesystem,
# never by running it.
macos_git_placeholder() {
  local developer link
  [ "$OS" = darwin ] || return 1
  [ "$1" = /usr/bin/git ] || return 1
  link="$(readlink /var/db/xcode_select_link 2>/dev/null || true)"
  for developer in "${DEVELOPER_DIR:-}" "$link" /Applications/Xcode.app/Contents/Developer \
      /Library/Developer/CommandLineTools; do
    [ -n "$developer" ] && [ -f "$developer/usr/bin/git" ] && return 1
  done
  return 0
}

# The git --source uses, as CLIO resolves it (runtime/git_executable.py): CLIO_GIT, a
# real git on PATH, else a bundled git (an installed CLIO Desktop's, or this prefix's
# release install's).
source_git() {
  local candidate runtime
  if [ -n "${CLIO_GIT:-}" ]; then
    if [ -x "$CLIO_GIT" ]; then printf '%s' "$CLIO_GIT"; fi
    return 0
  fi
  candidate="$(command -v git 2>/dev/null || true)"
  if [ -n "$candidate" ] && ! macos_git_placeholder "$candidate"; then
    printf '%s' "$candidate"
    return
  fi
  # A runtime's bundled git: CLIO Desktop's (macOS app, Linux deb/rpm) or the one a
  # release install of this prefix unpacked.
  for runtime in "${CLIO_DESKTOP_DIR:-$HOME/Applications}/CLIO Desktop.app/Contents/Resources/gact-runtime" \
      "/Applications/CLIO Desktop.app/Contents/Resources/gact-runtime" "/usr/lib/CLIO Desktop/gact-runtime" \
      "$PREFIX/clio-agent/.venv"; do
    candidate="$runtime/git/libexec/git-core/git"
    [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
  done
  # No git: an empty answer, not a failure, so `set -e` reaches the guidance below.
  return 0
}

# The shim that puts SOURCE_GIT first on PATH, for this script's git calls and uv's (the
# git-pinned dependencies). A bundled git must not use another git's helpers. On Linux its
# statically linked curl has no usable default CA bundle: it gets the distribution's, as
# clio_agent.runtime.git_executable gives it. A person's own git keeps its configuration.
write_git_shim() {
  local ca="" candidate
  case "$OS-$SOURCE_GIT" in
    linux-*/git/libexec/git-core/git)
      for candidate in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \
          /etc/ssl/ca-bundle.pem /etc/pki/tls/cacert.pem /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
          /etc/ssl/cert.pem; do
        [ -f "$candidate" ] && { ca="$candidate"; break; }
      done ;;
  esac
  {
    printf '#!/bin/sh\nunset GIT_EXEC_PATH\n'
    # shellcheck disable=SC2016  # expanded by the shim
    [ -z "$ca" ] || printf 'GIT_SSL_CAINFO="${GIT_SSL_CAINFO:-%s}"\nexport GIT_SSL_CAINFO\n' "$ca"
    printf 'exec "%s" "$@"\n' "$SOURCE_GIT"
  } > "$git_shim/git"
  chmod +x "$git_shim/git"
}

if [ "$OPT_SOURCE" = 1 ]; then
  SOURCE_GIT="$(source_git)"
  if [ -z "$SOURCE_GIT" ]; then
    [ "$OS" = darwin ] \
      && die "git is required for --source. This Mac has none (/usr/bin/git only offers the Command Line Tools). Install CLIO Desktop first (install.sh --desktop), whose git --source uses, or the Command Line Tools (xcode-select --install), or set CLIO_GIT"
    die "git is required for --source: install git, or install CLIO without --source first (its release install includes a git this one then uses), or set CLIO_GIT"
  fi
  if [ "$SOURCE_GIT" != "$(command -v git 2>/dev/null || true)" ]; then
    # This script's git calls and uv's (the git-pinned dependencies) find this git
    # first on PATH; a bundled one must not use another git's helpers.
    git_shim="$(mktemp -d "${TMPDIR:-/tmp}/clio-git.XXXXXXXX")"
    write_git_shim
    export PATH="$git_shim:$PATH"
    say "Using git: $SOURCE_GIT"
  fi
fi

# After the checks that can refuse this install (--source without git): a refused install
# would otherwise leave the uv it had started installing, unrecorded for uninstall.
# uv installs Python, the environment and the locked dependencies in every mode. It
# installs in the background: a release install starts downloading its prebuilt agent,
# which needs only curl, while uv arrives. need_uv waits for it before uv is first used.
mkdir -p "$PREFIX" "$BIN_DIR"
UV_JOB=""
UV_LOG=""
if ! have uv; then
  say "Installing uv into $BIN_DIR"
  UV_LOG="$(mktemp "${TMPDIR:-/tmp}/clio-uv.XXXXXXXX")"
  { curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH=1 sh >/dev/null; } \
    2> "$UV_LOG" &
  UV_JOB=$!
  export PATH="$BIN_DIR:$PATH"
fi
need_uv() {
  local status=0
  [ -n "$UV_JOB" ] || return 0
  wait "$UV_JOB" || status=$?
  UV_JOB=""
  cat "$UV_LOG" >&2 2>/dev/null || true
  rm -f "$UV_LOG"
  { [ "$status" = 0 ] && have uv; } \
    || die "could not install uv; install it (https://docs.astral.sh/uv/) and run this installer again"
  for f in uv uvx; do [ ! -f "$BIN_DIR/$f" ] || record_installed file "$BIN_DIR/$f"; done
}


# The checkout this script runs from, when it runs from one (not when streamed).
CHECKOUT=""
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  candidate="$(cd "$(dirname "$script_path")/.." && pwd)"
  if [ -f "$candidate/pyproject.toml" ] && grep -q '^name = "clio-agent"' "$candidate/pyproject.toml" \
     && [ -e "$candidate/.git" ]; then
    CHECKOUT="$candidate"
  fi
fi

# A ref's Python: its .python-version, or 3.13 for releases from before that file.
python_of() {
  local value
  value="$(tr -d '[:space:]' 2>/dev/null < "$1" || true)"
  printf '%s' "${value:-3.13}"
}

# ---------- the source clone: upgrade or repair on a re-run -----------

# A release install's environment that a source install replaces is set aside, not
# removed, until the source install has finished: an install that fails (no network, a
# ref that does not exist) puts it back, so the person keeps a working CLIO. The git this
# install chose can live in it (a release install's bundled git, for a machine with
# none): the bundled git is relocatable, and the shim follows it.
RELEASE_ASIDE=""
RELEASE_ORIGIN=""
set_release_aside() {  # DIR
  RELEASE_ORIGIN="$1"
  RELEASE_ASIDE="$1.release"
  rm -rf "$RELEASE_ASIDE"
  mv "$1" "$RELEASE_ASIDE" || die "could not set the release install in '$1' aside"
  [ -n "${git_shim:-}" ] || return 0
  case "$SOURCE_GIT" in
    "$1"/*)
      SOURCE_GIT="$RELEASE_ASIDE/${SOURCE_GIT#"$1"/}"
      write_git_shim ;;
  esac
}
# On exit: the release install set aside is put back unless the install finished (it
# then removed it). What replaced it is this run's own clone or link, holding no work.
restore_release_aside() {
  [ -n "$RELEASE_ASIDE" ] && [ -e "$RELEASE_ASIDE" ] || return 0
  if [ -L "$RELEASE_ORIGIN" ]; then rm -f "$RELEASE_ORIGIN"; else rm -rf "$RELEASE_ORIGIN"; fi
  mv "$RELEASE_ASIDE" "$RELEASE_ORIGIN" \
    && printf '%s\n' "The source install did not finish; the release install in '$RELEASE_ORIGIN' is back as it was." >&2
}
# The clone mode installs into $PREFIX/clio-agent. Running the installer again upgrades
# or repairs that clone in place; it never removes anything a person could have changed.

# Whether $1 holds this installer's clone to update (0), or nothing (1: absent, or what
# an earlier install left that holds no work: a partial clone an interrupted install
# left, a link to a checkout, a release install's environment, all removed here).
# Anything else is refused with its cause and the fix.
source_clone_reusable() {
  local src="$1" origin entry
  if [ -L "$src" ]; then
    say "Replacing the link to the checkout an in-place install used ($(readlink "$src")); the checkout is not touched"
    rm -f "$src"
    return 1
  fi
  [ -e "$src" ] || return 1
  if [ ! -e "$src/.git" ]; then
    for entry in "$src"/* "$src"/.[!.]*; do
      [ -e "$entry" ] || continue
      case "${entry##*/}" in
        .venv|.venv.previous|web) ;;
        *) die "Source reinstall refused: '$src' exists but is not a git clone (it holds '${entry##*/}'). Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
      esac
    done
    say "Replacing the release install's environment in '$src' (your data lives elsewhere)"
    set_release_aside "$src"
    return 1
  fi
  origin="$(git -C "$src" config --get remote.origin.url 2>/dev/null || true)"
  case "$origin" in
    https://github.com/iowarp/clio-agent|https://github.com/iowarp/clio-agent.git) ;;
    git@github.com:iowarp/clio-agent|git@github.com:iowarp/clio-agent.git) ;;
    "") die "Source reinstall refused: '$src' is a git repository with no 'origin', not a CLIO clone this installer made. Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
    *) die "Source reinstall refused: '$src' is a clone of $origin, not of $CLIO_REPO. Move it aside, or choose another CLIO_PREFIX, then run this installer again." ;;
  esac
  # An interrupted clone has no checked-out commit (or no index yet): nothing of yours
  # is in it, so it is cloned again.
  if ! git -C "$src" rev-parse -q --verify 'HEAD^{commit}' >/dev/null 2>&1 \
     || [ ! -f "$(git -C "$src" rev-parse --absolute-git-dir 2>/dev/null)/index" ]; then
    warn "'$src' is a clone an interrupted install left unfinished; cloning it again"
    rm -rf "$src"
    return 1
  fi
  return 0
}

# The clone's changes to tracked files, submodules included, one per line ("XY path").
# A submodule merely at another commit, nothing changed inside it, is what an
# interrupted clone or submodule update leaves, and the update below resets it: it is
# not a change. (Its commits, if any were made there, stay in that submodule.)
clone_changes() {
  git -C "$1" status --porcelain=v2 --untracked-files=no | awk '
    $1 == "1" && $2 == ".M" && $3 ~ /^SC\.[.U]$/ { next }
    $1 == "1" || $1 == "2" || $1 == "u" {
      skip = ($1 == "1") ? 8 : (($1 == "2") ? 9 : 10); path = $0
      for (i = 0; i < skip; i++) sub(/^[^ ]+ /, "", path)
      print $2 " " path
    }'
}

# Check out each submodule of the clone at $1 at the commit it records, recursively.
# This uses only commands every git has, for a git without `git submodule`, which is a
# shell script: CLIO Desktop's bundled git ships the git binary and its HTTPS helpers only.
checkout_submodules() {
  local src="$1" key path name url commit
  [ -f "$src/.gitmodules" ] || return 0
  git -C "$src" config -f .gitmodules --get-regexp '^submodule\..*\.path$' | while read -r key path; do
    name="${key#submodule.}"
    name="${name%.path}"
    url="$(git -C "$src" config -f .gitmodules --get "submodule.$name.url")" || exit 1
    commit="$(git -C "$src" ls-tree HEAD -- "$path" | awk '$2 == "commit" { print $3 }')"
    [ -n "$commit" ] || continue
    if [ ! -e "$src/$path/.git" ]; then
      git init -q "$src/$path" && git -C "$src/$path" remote add origin "$url" || exit 1
    fi
    if ! git -C "$src/$path" cat-file -e "$commit^{commit}" 2>/dev/null; then
      git -C "$src/$path" fetch -q --depth 1 origin "$commit" || exit 1
    fi
    git -C "$src/$path" checkout -q --detach "$commit" && checkout_submodules "$src/$path" || exit 1
  done
}

# `git submodule update --init --recursive` for the clone at $1 (further arguments go to
# it), or checkout_submodules when this git has no `git submodule`.
update_submodules() {
  local src="$1"
  shift
  if [ -e "$(git --exec-path)/git-submodule" ]; then
    git -C "$src" submodule update --init --recursive "$@"
  else
    checkout_submodules "$src"
  fi
}

# Bring this installer's clone at $1 to ref $2: fetch it, check it out detached, update
# the submodules. Local changes to tracked files (submodules included) refuse it.
update_source_clone() {
  local src="$1" ref="$2" dirty previous
  dirty="$(clone_changes "$src" 2>&1)" \
    || die "could not read the state of the clone '$src': $dirty"
  if [ -n "$dirty" ]; then
    die "Source reinstall refused: the clone '$src' has local changes to tracked files:
$dirty
Commit them on a branch or stash them (git -C '$src' stash), or move the clone aside, then run this installer again."
  fi
  previous="$(git -C "$src" rev-parse --short HEAD)"
  say "Updating the clio-agent clone in '$src' to $ref (was $previous)"
  git -C "$src" fetch -q --depth 1 origin "$ref" \
    || die "could not fetch '$ref' into '$src' (git's message is above): check that the branch, tag or commit exists and that this machine reaches GitHub, then run this installer again"
  git -C "$src" checkout -q --detach FETCH_HEAD \
    || die "could not check out '$ref' in '$src' (git's message is above; untracked files in the way are never removed: move them, then run this installer again)"
  # The caller updates the submodules (start_submodules), alongside the Python install.
}

# Release installs carry Web; source installs provide the service without a frontend build.
# The legacy TUI is frozen and unsupported. Do not build or download it here.
on_exit() {
  restore_release_aside
  [ -z "${lockdir:-}" ] || rm -rf "$lockdir"
  [ -z "${git_shim:-}" ] || rm -rf "$git_shim"
  [ -z "${tool_shim:-}" ] || rm -rf "$tool_shim"
  [ -z "${UV_LOG:-}" ] || rm -f "$UV_LOG"
  stop_install_jobs
}
trap on_exit EXIT

# ---------- work that overlaps the Python install ----------------------
# The managed runtime step (document_install, below) downloads LibreOffice (~220 MB)
# and pnpm itself unless they are handed to it, which used to start only after the
# Python install. The install plan of the CLIO being installed names those files with
# their pinned SHA-256 (scripts/release_install_plan.py, the plan --desktop uses); they
# are fetched while uv installs, into a folder named in CLIO_INSTALL_DOWNLOADS. The
# runtime uses a file only when its bytes match its own pin, waits for one still
# arriving, and fetches anything missing itself. A source clone's submodules are
# checked out at the same time too: no Python build reads them (tool.hatch exclude).
INSTALL_DOWNLOADS=""
SUBMODULE_JOB=""
submodule_failure=""

install_scratch() {
  [ -z "$DESKTOP_SCRATCH" ] || return 0
  DESKTOP_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/clio-install.XXXXXXXX")"
  mkdir "$DESKTOP_SCRATCH/downloads"
}

stop_install_jobs() {
  local pid
  for pid in ${UV_JOB:-} ${SUBMODULE_JOB:-} ${CLIO_KIT_JOB:-} ${RUNTIME_JOB:-} ${DOC_JOB:-} ${NODE_JOB:-} ${WEB_JOB:-} $DESKTOP_MONITOR $DESKTOP_JOBS; do
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
  [ -z "${RUNTIME_STAGE:-}" ] || rm -rf "$RUNTIME_STAGE"
  [ -z "$DESKTOP_SCRATCH" ] || rm -rf "$DESKTOP_SCRATCH"
  [ -z "${RUNTIME_STAGE:-}" ] || rm -rf "$RUNTIME_STAGE"
}

install_triple() {
  case "$OS-$ARCH" in
    linux-amd64) echo x86_64-unknown-linux-gnu ;;
    linux-arm64) echo aarch64-unknown-linux-gnu ;;
    darwin-arm64) echo aarch64-apple-darwin ;;
    darwin-amd64) echo x86_64-apple-darwin ;;
  esac
}

# The published plan of release TAG (authenticated by its SHA256SUMS), or nothing.
release_install_plan() {
  local triple
  triple="$(install_triple)"
  [ -n "$triple" ] || return 0
  if ! curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
      "$REPO_URL/releases/download/$1/SHA256SUMS.$triple.bundled.txt" -o "$DESKTOP_SCRATCH/checksums" 2>/dev/null; then
    warn "release $1 publishes no checksums for $triple: LibreOffice and pnpm download after the Python install"
    return 0
  fi
  if ! is_sha "$(manifest_sha "$DESKTOP_SCRATCH/checksums" "install-plan.$triple.bundled.txt")"; then
    warn "release $1 publishes no install plan for $triple: LibreOffice and pnpm download after the Python install"
    return 0
  fi
  fetch_plan "$1" "$DESKTOP_SCRATCH/checksums" "$triple"
}

# The plan of the checkout at SRC, from its own pins, run on PYTHON; or nothing.
source_install_plan() {
  local triple
  triple="$(install_triple)"
  [ -n "$triple" ] && [ -f "$1/scripts/release_install_plan.py" ] || return 0
  need_uv
  if uv run --quiet --no-project --python "$2" python -I "$1/scripts/release_install_plan.py" \
      --target "$triple" --no-sizes --output "$DESKTOP_SCRATCH/plan" >/dev/null 2>&1; then
    printf '%s' "$DESKTOP_SCRATCH/plan"
  else
    warn "could not read the install plan of '$1': LibreOffice and pnpm download after the Python install"
  fi
}

# start_install_downloads PLAN [KINDS]: fetch the plan's files of KINDS (default: office
# and npm, LibreOffice and pnpm) in the background. A plan's "runtime" archive is the
# desktop's own and not used here; its "browser" archives (Playwright's Node.js and
# Chromium) only a runtime install uses, as PyPI's Playwright brings its own Node.js.
start_install_downloads() {
  [ -n "$1" ] && [ -s "$1" ] || return 0
  local kinds="${2:-office npm}" part
  part="$DESKTOP_SCRATCH/plan.install.${kinds// /-}"
  awk -v kinds=" $kinds " '$1 == "capability" || index(kinds, " " $1 " ")' "$1" > "$part"
  start_prefetch "$part" office-first
  [ -n "$DESKTOP_JOBS" ] || return 0
  INSTALL_DOWNLOADS="$DESKTOP_SCRATCH/downloads"
  # A runtime that cannot wait for a download in progress would fetch it a second time.
  if plan_capable "$part" waits-for-partial-downloads; then
    DESKTOP_PLAN_WAITS=1
  fi
}

# The release's Web UI bundle, fetched while the managed packages install rather than
# after them; the Web step below waits for it and fetches it itself if this failed.
WEB_JOB=""
WEB_TAG=""
web_url() { printf 'https://github.com/iowarp/clio-agent/releases/download/%s/clio-web-%s.zip' "$1" "${1#v}"; }
start_web_download() {  # TAG
  WEB_TAG="$1"
  curl -fsSL --retry 3 "$(web_url "$1")" -o "$DESKTOP_SCRATCH/clio-web.zip" 2>/dev/null &
  WEB_JOB=$!
}

# ---------- the release's prebuilt agent (Apple Silicon macOS) ---------
# A release whose macOS desktop bundles its agent carries that agent's runtime: the
# same Python, packages, compiled bytecode, uv, git and document wheels, signed and
# notarized with the app. A plain install unpacks that runtime as its environment while
# the app archive downloads, instead of resolving and writing ~26,000 files from PyPI
# (gact-tui#739: a cold PyPI install took 92-108 s, the 60 s budget). Only the runtime
# folder is unpacked, never the app. Without the asset, or if it cannot be fetched and
# verified, the install continues from PyPI. CLIO_INSTALL_FROM=pypi always uses PyPI;
# CLIO_RUNTIME_PACKAGE (a local bundled .app.aar or .app.tar.gz, as --desktop-package)
# with CLIO_RUNTIME_SHA256 tries a test build before its release exists.
RUNTIME_JOB=""
RUNTIME_STAGE=""
RUNTIME_SHA=""
RUNTIME_INNER="CLIO Desktop.app/Contents/Resources/gact-runtime"

release_runtime_asset() {  # CHECKSUMS -> the bundled app archive holding the runtime
  local stem="CLIO.Desktop-aarch64-apple-darwin-bundled.app" format
  for format in aar tar.gz; do
    is_sha "$(manifest_sha "$1" "$stem.$format")" || continue
    printf '%s' "$stem.$format"
    return
  done
}

runtime_unpack() {  # ASSET DIR: the runtime folder of the archive on stdin
  case "$1" in
    *.aar) aa extract -d "$2" -include-path "$RUNTIME_INNER" ;;
    *) tar -xzf - -C "$2" "$RUNTIME_INNER" ;;
  esac
}

# stream_runtime SOURCE: unpack as it arrives; the same bytes are hashed (runtime.sha).
stream_runtime() {
  local fifo="$DESKTOP_SCRATCH/runtime.fifo" hasher status
  rm -f "$fifo"
  mkfifo "$fifo"
  { sha_stream > "$DESKTOP_SCRATCH/runtime.sha"; } < "$fifo" &
  hasher=$!
  case "$1" in
    https://*) curl --proto '=https' --tlsv1.2 -fsSL --speed-limit 1 --speed-time 60 "$1" ;;
    *) cat "$1" ;;
  esac | tee "$fifo" | runtime_unpack "$1" "$RUNTIME_STAGE"
  status=("${PIPESTATUS[@]}")
  wait "$hasher" || return 1
  [ "${status[0]}" = 0 ] && [ "${status[1]}" = 0 ] && [ "${status[2]}" = 0 ]
}

start_release_runtime() {  # TAG
  local sums="$DESKTOP_SCRATCH/checksums" asset source
  [ "$OS-$ARCH" = darwin-arm64 ] && [ "${CLIO_INSTALL_FROM:-}" != pypi ] || return 0
  [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ] || return 0
  if [ -n "${CLIO_RUNTIME_PACKAGE:-}" ]; then
    [ -f "$CLIO_RUNTIME_PACKAGE" ] || die "no such runtime package: $CLIO_RUNTIME_PACKAGE"
    RUNTIME_SHA="$(printf '%s' "${CLIO_RUNTIME_SHA256:-}" | tr 'A-F' 'a-f')"
    is_sha "$RUNTIME_SHA" || die "CLIO_RUNTIME_PACKAGE needs CLIO_RUNTIME_SHA256 (64 hex digits)"
    asset="${CLIO_RUNTIME_PACKAGE##*/}"
    source="$CLIO_RUNTIME_PACKAGE"
  else
    # release_install_plan has fetched them when the release publishes them.
    [ -s "$sums" ] || curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
      "$REPO_URL/releases/download/$1/SHA256SUMS.aarch64-apple-darwin.bundled.txt" -o "$sums" 2>/dev/null || true
    asset=""
    [ ! -s "$sums" ] || asset="$(release_runtime_asset "$sums")"
    if [ -z "$asset" ]; then
      say "Release $1 publishes no prebuilt agent for macOS: installing it from PyPI"
      return 0
    fi
    RUNTIME_SHA="$(manifest_sha "$sums" "$asset" | tr 'A-F' 'a-f')"
    source="$REPO_URL/releases/download/$1/$asset"
  fi
  # Beside the environment it replaces, so moving it in is a rename.
  mkdir -p "$PREFIX/clio-agent"
  RUNTIME_STAGE="$PREFIX/clio-agent/.venv.incoming"
  rm -rf "$RUNTIME_STAGE"
  mkdir "$RUNTIME_STAGE"
  case "$asset" in
    *.aar|*.tar.gz) ;;
    *) die "a runtime package is a bundled .app.aar or .app.tar.gz: $asset" ;;
  esac
  say "Downloading the release's prebuilt agent ($asset), unpacking its runtime as it arrives"
  stream_runtime "$source" > "$DESKTOP_SCRATCH/runtime.log" 2>&1 &
  RUNTIME_JOB=$!
}

# write_runtime_launchers ROOT PYTHON: ROOT/bin gets a launcher running PYTHON, written as
# uv writes it (so a path with spaces works), for every console and GUI command the
# runtime's packages declare: what a PyPI install of the same packages has. The first
# declaration of a name wins and a file already in bin/ (uv) is kept. The lite desktop
# starts its backend with clio-agent-gact; an install without it never starts
# (gact-tui#739). 1 when there is no clio-agent command.
write_runtime_launchers() {
  local root="$1" script name module attr written=()
  script="$(printf '%s' "$2" | sed "s/'/'\\\\''/g")"
  while read -r name module attr; do
    [ ! -e "$root/bin/$name" ] && [ ! -L "$root/bin/$name" ] || continue
    printf "#!/bin/sh\n'''exec' '%s' \"\$0\" \"\$@\"\n' '''\nimport sys\nfrom %s import %s\nif __name__ == \"__main__\":\n    sys.exit(%s())\n" \
      "$script" "$module" "${attr%%.*}" "$attr" > "$root/bin/$name" || return 1
    written+=("$root/bin/$name")
  done < <(awk '
    FNR == 1 { section = "" }
    /^[[:space:]]*\[/ { section = $0; gsub(/[][[:space:]]/, "", section); next }
    (section == "console_scripts" || section == "gui_scripts") && index($0, "=") {
      name = substr($0, 1, index($0, "=") - 1); value = substr($0, index($0, "=") + 1)
      gsub(/[[:space:]]/, "", name); sub(/\[.*$/, "", value); gsub(/[[:space:]]/, "", value)
      split(value, part, ":")
      if (name ~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ && part[1] ~ /^[A-Za-z_][A-Za-z0-9_.]*$/ \
          && part[2] ~ /^[A-Za-z_][A-Za-z0-9_.]*$/ && !(name in seen)) {
        seen[name] = 1; print name, part[1], part[2]
      }
    }' "$root"/python/lib/python3.*/site-packages/*.dist-info/entry_points.txt 2>/dev/null)
  [ "${#written[@]}" = 0 ] || chmod 755 "${written[@]}"
  [ -x "$root/bin/clio-agent" ]
}

# finish_release_runtime VERSION: the runtime becomes $VENV; 1 means install from PyPI.
finish_release_runtime() {
  local status=0 root python version previous=""
  [ -n "$RUNTIME_JOB" ] || return 1
  wait "$RUNTIME_JOB" || status=$?
  RUNTIME_JOB=""
  root="$RUNTIME_STAGE/$RUNTIME_INNER"
  if [ "$status" != 0 ] || [ "$(cat "$DESKTOP_SCRATCH/runtime.sha" 2>/dev/null)" != "$RUNTIME_SHA" ] \
      || [ ! -f "$root/runtime.json" ]; then
    sed 's/^/    /' "$DESKTOP_SCRATCH/runtime.log" 2>/dev/null >&2 || true
    warn "could not download and verify the release's prebuilt agent: installing it from PyPI instead"
    rm -rf "$RUNTIME_STAGE"
    return 1
  fi
  python="$(cd "$root/python/bin" && ls python3.* 2>/dev/null | grep -v -- -config | head -1)"
  # The environment's usual entry points: bin/python, and the commands a PyPI install
  # writes (clio-agent, which `clio` runs; clio-agent-gact, which the desktop runs).
  if [ -z "$python" ] || ! ln -s "../python/bin/$python" "$root/bin/python" \
      || ! ln -s python "$root/bin/python3"; then
    warn "the release's prebuilt agent has no Python: installing it from PyPI instead"
    rm -rf "$RUNTIME_STAGE"
    return 1
  fi
  if ! write_runtime_launchers "$root" "$VENV/bin/python"; then
    warn "the release's prebuilt agent declares no clio-agent command: installing it from PyPI instead"
    rm -rf "$RUNTIME_STAGE"
    return 1
  fi
  version="$("$root/bin/python" -I -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
  if [ "$version" != "$1" ]; then
    if [ -z "${CLIO_RUNTIME_PACKAGE:-}" ]; then
      warn "the release's prebuilt agent is clio-agent ${version:-of no version}, not $1: installing it from PyPI instead"
      rm -rf "$RUNTIME_STAGE"
      return 1
    fi
    warn "the runtime package is clio-agent ${version:-of no version}, not $1 (a test build)"
  fi
  if [ -d "$VENV" ]; then
    previous="$VENV.previous"
    rm -rf "$previous"
    mv "$VENV" "$previous"
  fi
  if ! mv "$root" "$VENV"; then
    [ -z "$previous" ] || mv "$previous" "$VENV"
    die "could not move the prebuilt agent into $VENV${previous:+; the previous installation is unchanged}"
  fi
  rm -rf "$RUNTIME_STAGE" ${previous:+"$previous"}
  RUNTIME_STAGE=""
  say "Installed clio-agent ${version:-$1} from the release's prebuilt agent"
}

# ---------- clio-kit MCP runtime, provisioned alongside the Python install
# Marketplace packs launch their MCP servers via the installed `clio-kit
# mcp-server <name>` launcher. The default matches this CLIO release's Kit pin;
# explicit legacy shared-runtime candidates select their science dependency union.
# Installed-tool launchers (not per-server `uvx` calls)
# avoid the concurrent cold-cache ephemeral-env race and `uv cache prune`
# deleting envs under running servers (astral-sh/uv#11694). `uv tool install`
# is idempotent (re-run is a no-op / version pin). Needs uv; without it the
# packs' tools simply stay unprovisioned until uv is installed. It needs only uv and
# the release's Python, so it runs while clio-agent installs (start_clio_kit PYTHON),
# never while the managed runtime step's cache collection looks for live clio-kit.
CLIO_KIT_JOB=""
clio_kit_spec=""
# A CLIO with its own prefix, home, data or user folder keeps clio-kit in uv tool
# folders inside its prefix, so it never replaces the user's own clio-kit; the launcher
# puts them on the agent's PATH and uninstall.sh removes them with the payloads. A
# caller that chose uv's tool folders itself (an SSH deployment) keeps its choice.
KIT_SCOPED=""
KIT_TOOL_DIR=""
if [ -n "${CLIO_PREFIX:-}${CLIO_AGENT_HOME:-}${CLIO_AGENT_DATA_DIR:-}${CLIO_USER_DIR:-}" ]; then
  KIT_SCOPED=1
  [ -n "${UV_TOOL_DIR:-}" ] || KIT_TOOL_DIR="$PREFIX/uv-tools"
fi
kit_uv() {  # uv with this installation's tool folders, for that command only
  if [ -n "$KIT_TOOL_DIR" ]; then
    # On PATH for uv only, which otherwise warns that it is not; the launcher adds it.
    PATH="$KIT_TOOL_DIR/bin:$PATH" UV_TOOL_DIR="$KIT_TOOL_DIR" \
      UV_TOOL_BIN_DIR="$KIT_TOOL_DIR/bin" uv "$@"
  else
    uv "$@"
  fi
}
start_clio_kit() {
  need_uv
  if ! have uv; then
    warn "uv not found — skipping clio-kit MCP runtime provisioning; install uv and rerun this installer"
    return 0
  fi
  if [ -z "$CLIO_KIT_PACKAGE" ]; then
    clio_kit_spec="clio-kit==2.12.7"
    say "Provisioning released clio-kit launcher ($clio_kit_spec) at the same time"
  elif [[ "$CLIO_KIT_PACKAGE" == *"=="* ]]; then
    clio_kit_spec="${CLIO_KIT_PACKAGE%%==*}[science]==${CLIO_KIT_PACKAGE#*==}"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec) at the same time"
  else
    clio_kit_spec="${CLIO_KIT_PACKAGE}[science]"
    say "Provisioning candidate shared clio-kit science runtime ($clio_kit_spec) at the same time"
  fi
  # Recorded for uninstall unless the user already had it: the tool's name is the spec
  # without its version and extras (a path or URL spec has none to record). A scoped
  # installation's tool folders go with its payloads instead; one made before scoping
  # keeps the record of the shared clio-kit its installer added.
  local tool="${clio_kit_spec%%[=\[<>~! ]*}"
  mkdir -p "$PREFIX"
  if [[ "$tool" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] && { [ "$UNRECORDED_INSTALL" = 1 ] \
      || { [ -z "$KIT_SCOPED" ] && ! uv tool list 2>/dev/null | grep -q "^$tool "; }; }; then
    record_installed uv-tool "$tool"
  fi
  # Even empty, the record tells uninstall.sh this installer said what it added, so it
  # never takes the user's own clio-kit for one an older installer put there.
  : >> "$INSTALLED_TOOLS"
  [ -z "$KIT_TOOL_DIR" ] || say "clio-kit goes in this installation's own tool folder: $KIT_TOOL_DIR"
  kit_uv tool install --python "$1" "$clio_kit_spec" > "$DESKTOP_SCRATCH/clio-kit.log" 2>&1 &
  CLIO_KIT_JOB=$!
}
finish_clio_kit() {
  local status=0
  [ -n "$CLIO_KIT_JOB" ] || return 0
  wait "$CLIO_KIT_JOB" || status=$?
  CLIO_KIT_JOB=""
  cat "$DESKTOP_SCRATCH/clio-kit.log" 2>/dev/null || true
  local retry="uv tool install \"$clio_kit_spec\""
  [ -z "$KIT_TOOL_DIR" ] || retry="UV_TOOL_DIR=\"$KIT_TOOL_DIR\" UV_TOOL_BIN_DIR=\"$KIT_TOOL_DIR/bin\" $retry"
  [ "$status" = 0 ] && say "clio-kit launcher ready ($clio_kit_spec)" \
    || warn "clio-kit provisioning failed; marketplace pack tools will be unavailable until '$retry' succeeds and '\$(uv tool dir --bin)' is on PATH"
}

# start_submodules SRC [ARGS]: update_submodules in the background; finish_submodules
# MESSAGE waits for it, shows git's output and dies with MESSAGE if it failed.
start_submodules() {
  local log="$DESKTOP_SCRATCH/submodules.log"
  ( update_submodules "$@" ) > "$log" 2>&1 &
  SUBMODULE_JOB=$!
}
finish_submodules() {
  local status=0
  [ -n "$SUBMODULE_JOB" ] || return 0
  wait "$SUBMODULE_JOB" || status=$?
  SUBMODULE_JOB=""
  cat "$DESKTOP_SCRATCH/submodules.log" 2>/dev/null || true
  [ "$status" = 0 ] || die "$1"
}

# ---------- a release's prebuilt runtime (Linux) ----------------------
# A Linux release publishes the desktop's runtime archive (gact-runtime-<version>-linux-
# <arch>.tar.zst, named with its SHA-256 by the bundled install plan): clio-agent and
# every locked dependency, installed and compiled once in CI, and the document
# environment's wheels. A release install takes the packages from it instead of
# resolving and downloading them from PyPI, unpacking them while the archive downloads,
# into the virtualenv a PyPI install makes: same layout, so the launcher, `clio update`,
# the data check and uninstall work as they do. The archive's document wheels go to the
# managed-package step (CLIO_INSTALL_DOWNLOADS), which checks each against its lock.
# A release without the archive, or a failure, installs from PyPI;
# CLIO_INSTALL_FROM=pypi chooses PyPI.
RUNTIME_UNPACK=""
DOC_JOB=""
NODE_JOB=""

# runtime_plan_entry PLAN: "sha bytes name url" of the plan's runtime archive, or nothing.
runtime_plan_entry() {
  [ "$OS" = linux ] && [ "${CLIO_INSTALL_FROM:-}" != pypi ] && [ -n "$1" ] && [ -s "$1" ] || return 0
  awk '$1 == "runtime" && $4 ~ /^gact-runtime-[A-Za-z0-9._+-]*-linux-[a-z0-9]+\.tar\.zst$/' "$1" \
    > "$DESKTOP_SCRATCH/plan.runtime"
  [ "$(plan_entries "$DESKTOP_SCRATCH/plan.runtime" | wc -l)" = 1 ] || return 0
  plan_entries "$DESKTOP_SCRATCH/plan.runtime"
}

# Reads a zstd stream on stdin and writes the tar it holds, hashing the compressed bytes
# as they pass; once the stream ends on a complete frame it writes "sha256 bytes" to
# argv[1]. A fresh Linux has no zstd command; uv's Python 3.14 decodes it.
RUNTIME_DECODER='
import hashlib, sys
from compression import zstd
def fresh():
    return zstd.ZstdDecompressor(options={zstd.DecompressionParameter.window_log_max: 31})
source, sink = sys.stdin.buffer, sys.stdout.buffer
digest, size, decoder, ended = hashlib.sha256(), 0, fresh(), False
while chunk := source.read(1 << 20):
    digest.update(chunk)
    size += len(chunk)
    while chunk:
        sink.write(decoder.decompress(chunk))
        ended = decoder.eof
        chunk = decoder.unused_data if ended else b""
        if ended:
            decoder = fresh()
sink.flush()
if not ended:
    sys.exit("the runtime archive ends inside a zstd frame")
with open(sys.argv[1], "w", encoding="ascii") as out:
    out.write(f"{digest.hexdigest()} {size}\n")
'

# Moves the runtime's packages (argv[1]) into the virtualenv's (argv[2]) and writes, to
# argv[3], a wheel of launchers for every entry point they declare: uv installs it
# offline, writing bin/ as it does for the same packages from PyPI. The runtime keeps
# no bin/ scripts: theirs named the build machine's interpreter.
RUNTIME_FINISHER='
import base64, hashlib, re, sys, zipfile
from pathlib import Path
source, site, wheel = (Path(arg) for arg in sys.argv[1:4])
for entry in sorted(source.iterdir()):
    if (site / entry.name).exists():
        sys.exit(f"{entry.name} is already in the environment")
    entry.rename(site / entry.name)
sections = {"console_scripts": {}, "gui_scripts": {}}
for info in sorted(site.glob("*.dist-info")):
    # Built from a CI checkout: a PyPI install records no local source.
    direct = info / "direct_url.json"
    if direct.is_file():
        direct.unlink()
        record = info / "RECORD"
        if record.is_file():
            lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
            record.write_text(
                "".join(line for line in lines if not line.startswith(f"{info.name}/direct_url.json,")),
                encoding="utf-8",
            )
    points = info / "entry_points.txt"
    if not points.is_file():
        continue
    section = ""
    for line in points.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if match := re.fullmatch(r"\[(.+)\]", line):
            section = match[1]
        elif section in sections and (match := re.fullmatch(r"([^=\s]+)\s*=\s*(.+)", line)):
            sections[section].setdefault(match[1], match[2])
if "clio-agent" not in sections["console_scripts"]:
    sys.exit("the runtime declares no clio-agent entry point")
info = "clio_runtime_launchers-1.dist-info"
files = {
    f"{info}/METADATA": "Metadata-Version: 2.1\nName: clio-runtime-launchers\nVersion: 1\n"
    "Summary: Launchers for the CLIO runtime this environment was installed from\n",
    f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: clio-install\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    f"{info}/entry_points.txt": "".join(
        f"[{name}]\n" + "".join(f"{key} = {value}\n" for key, value in entries.items()) + "\n"
        for name, entries in sections.items()
        if entries
    ),
}
record = []
with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, text in files.items():
        data = text.encode()
        archive.writestr(name, data)
        sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={sha},{len(data)}\n")
    archive.writestr(f"{info}/RECORD", "".join(record) + f"{info}/RECORD,,\n")
'

# install_runtime_archive SHA BYTES NAME URL PYTHON: $VENV from the release's runtime
# archive (0), or nothing installed and a reason on stderr (1).
install_runtime_archive() {
  local sha="$1" bytes="$2" name="$3" url="$4" python="$5" version site status sum f
  RUNTIME_UNPACK="$PREFIX/clio-agent/.runtime-unpack"
  rm -rf "$RUNTIME_UNPACK"
  mkdir -p "$RUNTIME_UNPACK"
  need_uv
  uv venv --quiet --python "$python" "$VENV" || { warn "uv could not create $VENV"; return 1; }
  version="$("$VENV/bin/python" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 1
  if ! "$VENV/bin/python" -I -c 'import compression.zstd' 2>/dev/null; then
    warn "Python $version here cannot decode zstd"
    return 1
  fi
  site="$VENV/lib/python$version/site-packages"
  say "Downloading and unpacking the CLIO runtime ($name, $(megabytes "$bytes") MB)"
  # Only the packages, the document wheels, uv and git: the runtime's own Python is not used.
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --speed-limit 1 --speed-time 60 "$url" \
    | "$VENV/bin/python" -I -c "$RUNTIME_DECODER" "$RUNTIME_UNPACK/sum" \
    | tar -x --no-same-owner -C "$RUNTIME_UNPACK" -f - \
        "gact-runtime/python/lib/python$version/site-packages" gact-runtime/document-wheels gact-runtime/bin \
        gact-runtime/git \
    && status=("${PIPESTATUS[@]}") || status=("${PIPESTATUS[@]}")
  if [ "${status[*]}" != "0 0 0" ]; then
    warn "downloading or unpacking $name failed (curl, decoder, tar: ${status[*]})"
    return 1
  fi
  sum="$(cat "$RUNTIME_UNPACK/sum" 2>/dev/null || true)"
  # A plan written without sizes says 0; the SHA-256 is what authenticates the archive.
  if [ "${sum%% *}" != "$sha" ] || { [ "$bytes" != 0 ] && [ "${sum#* }" != "$bytes" ]; }; then
    warn "$name does not match the release's checksum"
    return 1
  fi
  # The line is free now: the browser archives the runtime pins download while it is put
  # in place, and the browser step of the package install finds them on disk.
  start_install_downloads "${install_plan:-}" browser
  "$VENV/bin/python" -I -c "$RUNTIME_FINISHER" "$RUNTIME_UNPACK/gact-runtime/python/lib/python$version/site-packages" \
    "$site" "$RUNTIME_UNPACK/clio_runtime_launchers-1-py3-none-any.whl" || return 1
  uv pip install --quiet --offline --no-deps --python "$VENV/bin/python" \
    "$RUNTIME_UNPACK/clio_runtime_launchers-1-py3-none-any.whl" || { warn "uv could not install the CLIO launchers"; return 1; }
  # A PyPI environment has uv beside its Python too (the uv package's own scripts).
  for f in uv uvx; do
    [ -e "$VENV/bin/$f" ] || [ ! -f "$RUNTIME_UNPACK/gact-runtime/bin/$f" ] || mv "$RUNTIME_UNPACK/gact-runtime/bin/$f" "$VENV/bin/$f"
  done
  # The relocatable git CLIO installs marketplace plugins with, for a machine without
  # one; clio_agent.runtime.git_executable finds it beside the environment's Python.
  mv "$RUNTIME_UNPACK/gact-runtime/git" "$VENV/git" || { warn "could not place the runtime's git"; return 1; }
  if [ -f "$RUNTIME_UNPACK/gact-runtime/document-wheels/manifest.json" ]; then
    [ -n "$INSTALL_DOWNLOADS" ] || INSTALL_DOWNLOADS="$DESKTOP_SCRATCH/downloads"
    mkdir -p "$INSTALL_DOWNLOADS"
    ln -sfn "$RUNTIME_UNPACK/gact-runtime/document-wheels" "$INSTALL_DOWNLOADS/document-wheels"
  fi
}

has_module() {  # MODULE [TEXT]: the installed clio-agent has MODULE (whose source has TEXT)
  "$VENV/bin/python" -I -c 'import importlib.util, sys
spec = importlib.util.find_spec(sys.argv[1])
sys.exit(0 if spec and (len(sys.argv) < 3 or sys.argv[2] in open(spec.origin, encoding="utf-8").read()) else 3)' "$@" 2>/dev/null
}
# PROVIDER_SUPPORT names provider SDKs to lay down with the managed packages, so a first
# turn never installs one; a clio-agent without the step installs them on first use.
wants_provider_support() {
  [ -n "$PROVIDER_SUPPORT" ] && has_module clio_agent.runtime.document_install --provider-support
}

# The managed packages and Office are the longest step and read nothing the data check
# and the store migration write, so a runtime install starts them at once.
start_document_install() {
  local flags=()
  "$VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.runtime.document_install") and importlib.util.find_spec("clio_agent.gact.store_migrate_command") else 3)' 2>/dev/null \
    || return 0
  say 'Installing managed Python/uv and Node/pnpm packages and Office rendering (alongside the steps below)'
  flags=(--skip-store-migrations)
  ! wants_provider_support || flags+=(--provider-support "$PROVIDER_SUPPORT")
  # A runtime that cannot wait for a download still arriving gets it once it has landed.
  [ "$DESKTOP_PLAN_WAITS" = 1 ] || finish_downloads
  env ${INSTALL_DOWNLOADS:+"CLIO_INSTALL_DOWNLOADS=$INSTALL_DOWNLOADS"} \
    "$VENV/bin/python" -m clio_agent.runtime.document_install "${flags[@]}" > "$DESKTOP_SCRATCH/documents.log" 2>&1 &
  DOC_JOB=$!
  # PyPI's Playwright carries its Node.js driver; the runtime's fetches it on first use.
  "$VENV/bin/python" -c 'from clio_agent.gact.browser.driver import ensure_node; ensure_node()' \
    > "$DESKTOP_SCRATCH/browser-driver.log" 2>&1 &
  NODE_JOB=$!
}

# ---------- install clio-agent ----------------------------------------
VENV="$PREFIX/clio-agent/.venv"
SRC=""
# The CLIO this install replaces, named if the data check below cannot tell.
PREVIOUS_VERSION=""
if [ -x "$VENV/bin/python" ]; then
  PREVIOUS_VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
fi

if [ "$OPT_SOURCE" = 1 ]; then
  if [ -n "$CHECKOUT" ] && [ -z "$OPT_REF" ]; then
    SRC="$CHECKOUT"
    say "Installing the checkout in place: $SRC ($(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached))"
    if [ -L "$PREFIX/clio-agent" ] || [ ! -e "$PREFIX/clio-agent" ]; then
      :
    elif [ -e "$PREFIX/clio-agent/.git" ]; then
      die "Source reinstall refused: '$PREFIX/clio-agent' is a clone that may hold your changes. Move it, or choose another CLIO_PREFIX."
    else
      set_release_aside "$PREFIX/clio-agent"  # a release install's environment; data lives elsewhere
    fi
    ln -sfn "$SRC" "$PREFIX/clio-agent"
    install_scratch
    start_submodules "$SRC"
    submodule_failure="could not update the submodules of '$SRC' (git's message is above); run this installer again"
    py="$(python_of "$SRC/.python-version")"
    sync_args=(sync --python "$py" --extra argonne --extra flowcept)
  else
    ref="${OPT_REF:-$([ "$OPT_DEV" = 1 ] && echo develop || echo main)}"
    SRC="$PREFIX/clio-agent"
    install_scratch
    if source_clone_reusable "$SRC"; then
      update_source_clone "$SRC" "$ref"
      start_submodules "$SRC" --depth 1
      submodule_failure="could not update the submodules of '$SRC' to '$ref' (git's message is above); run this installer again"
    else
      say "Cloning clio-agent at $ref"
      if ! git clone --quiet --branch "$ref" --depth 1 "$CLIO_REPO" "$SRC" 2>/dev/null; then
        # A commit is not a branch or tag: fetch it by id.
        rm -rf "$SRC"
        git init -q "$SRC" && git -C "$SRC" remote add origin "$CLIO_REPO" \
          && git -C "$SRC" fetch -q --depth 1 origin "$ref" && git -C "$SRC" checkout -q FETCH_HEAD \
          || die "could not fetch clio-agent at '$ref' from $CLIO_REPO: check that the branch, tag or commit exists and that this machine reaches GitHub, then run this installer again"
      fi
      start_submodules "$SRC" --depth 1
      submodule_failure="could not fetch the submodules of clio-agent at '$ref' (git's message is above); run this installer again"
    fi
    py="$(python_of "$SRC/.python-version")"
    sync_args=(sync --locked --python "$py" --extra argonne --extra flowcept)
  fi
  need_uv  # before the subshell below, which cannot wait for this shell's job
  start_install_downloads "$(source_install_plan "$SRC" "$py")"
  start_clio_kit "$py"
  say "Installing clio-agent deps (uv ${sync_args[*]})"
  sync_status=0
  ( cd "$SRC" && uv "${sync_args[@]}" ) || sync_status=$?
  finish_submodules "$submodule_failure"
  [ "$sync_status" = 0 ] \
    || die_with "$sync_status" "uv could not install clio-agent's dependencies (exit $sync_status; uv's message above names the cause). Fix it and run this installer again."
else
  if [ -n "$OPT_VERSION" ]; then
    package_version="$OPT_VERSION"
    if [[ "$package_version" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)\.([0-9]+)$ ]]; then
      package_version="${BASH_REMATCH[1]}b${BASH_REMATCH[2]}.post${BASH_REMATCH[3]}"
    elif [[ "$package_version" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)-beta\.([0-9]+)$ ]]; then
      package_version="${BASH_REMATCH[1]}b${BASH_REMATCH[2]}"
    fi
    package_version="${package_version#v}"
  else
    # The published GitHub Releases are the release list (PyPI can hold strays that
    # never became a release). The default takes the highest stable version; --dev the
    # most recently published release, pre-releases included.
    which="stable"; [ "$OPT_DEV" = 1 ] && which="any"
    say "Resolving the latest $([ "$which" = any ] && echo release || echo stable release)"
    need_uv
    # Compressed: the list is ~8.6 MB of JSON. Python 3.14 is the one the release pins
    # (.python-version) and that the runtime archive decoder uses, so uv downloads it once.
    package_version="$(curl -fsSL --compressed "https://api.github.com/repos/iowarp/clio-agent/releases?per_page=100" \
      | uv run --quiet --no-project --python 3.14 --with packaging python -c '
import json, re, sys
from packaging.version import InvalidVersion, Version
found = []
for release in json.load(sys.stdin):
    if release.get("draft"):
        continue
    tag = release.get("tag_name", "")
    match = re.fullmatch(r"v(\d+\.\d+\.\d+(?:\.\d+)?)-beta\.(\d+)(?:\.(\d+))?", tag)
    text = (f"{match[1]}b{match[2]}" + (f".post{match[3]}" if match[3] else "")) if match else tag[1:]
    try:
        version = Version(text)
    except InvalidVersion:
        continue  # not a clio-agent release tag
    if sys.argv[1] == "stable" and version.is_prerelease:
        continue
    found.append((release.get("published_at") or "", version))
# Stable: the highest stable version. --dev: the most recently published release,
# whatever its version says (an old alpha can sort above the newest beta).
print(max(v for _, v in found) if sys.argv[1] == "stable" else max(found)[1])
' "$which")" || die "could not read the clio-agent releases from GitHub"
  fi
  tag="$(release_tag "$package_version")"
  install_scratch
  install_plan="$(release_install_plan "$tag")"
  start_release_runtime "$tag"
  # The prebuilt agent is the critical path, so it downloads alone; LibreOffice and pnpm
  # follow while the store migrates (sharing the line, all three finished ~25 s later on
  # a 15 MB/s link). A PyPI install leaves the line free for them from the start.
  runtime_first=0
  if [ -n "$RUNTIME_JOB" ]; then runtime_first=1; else start_install_downloads "$install_plan"; fi
  runtime_entry="$(runtime_plan_entry "$install_plan")"
  lockdir="$(mktemp -d)"  # removed on exit (on_exit)
  for f in pyproject.toml uv.lock .python-version; do
    curl -fsSL "https://raw.githubusercontent.com/iowarp/clio-agent/$tag/$f" -o "$lockdir/$f" 2>/dev/null \
      || { [ "$f" = .python-version ] || die "release $tag has no $f on GitHub"; }
  done
  py="$(python_of "$lockdir/.python-version")"
  need_uv
  ( cd "$lockdir" && uv export --quiet --frozen --no-hashes --no-emit-project \
      --extra argonne --extra flowcept -o constraints.txt ) \
    || die "could not read the dependency lock of release $tag"
  start_clio_kit "$py"
  runtime_installed=0
  ! finish_release_runtime "$package_version" || runtime_installed=1
  # The browser archives (Node.js and Chromium, from the runtime's pin) only the prebuilt agent uses.
  if [ "$runtime_first" = 1 ] && [ "$runtime_installed" = 1 ]; then
    start_install_downloads "$install_plan" "office npm browser"
  elif [ "$runtime_first" = 1 ]; then
    start_install_downloads "$install_plan"
  fi
  start_web_download "$tag"
  if [ "$runtime_installed" = 1 ]; then
    CLIO_VERSION="$package_version"
  else
    pkg_spec="clio-agent[argonne,flowcept]==$package_version"
    # On macOS, select an available Rasterio wheel for the user's OS instead
    # of trying to compile a newer release against a missing system GDAL.
    wheel_arg=""
    [ "$OS" != darwin ] || wheel_arg="--only-binary=rasterio"
    # The installed environment is set aside, not deleted, until the new one is in
    # place: an install that fails or is interrupted puts it back.
    previous=""
    if [ -d "$VENV" ]; then
      previous="$VENV.previous"
      rm -rf "$previous"
      mv "$VENV" "$previous"
    fi
    mkdir -p "$PREFIX/clio-agent"
    from_runtime=0
    if [ -n "$runtime_entry" ]; then
      # shellcheck disable=SC2086  # "sha bytes name url", all checked by plan_entries
      if install_runtime_archive $runtime_entry "$py"; then
        from_runtime=1
        RUNTIME_SHA="${runtime_entry%% *}"
        start_document_install
      else
        rm -rf "$VENV" "$RUNTIME_UNPACK"
        [ -z "$INSTALL_DOWNLOADS" ] || rm -f "$INSTALL_DOWNLOADS/document-wheels"
        warn "could not install from release $tag's runtime archive; installing from PyPI instead"
      fi
    fi
    if [ "$from_runtime" = 0 ]; then
      say "Installing $pkg_spec from PyPI (Python $py, locked by $tag)"
      if ! { uv venv --python "$py" "$VENV" >/dev/null \
          && uv pip install --quiet --python "$VENV/bin/python" ${wheel_arg:+"$wheel_arg"} \
            --constraint "$lockdir/constraints.txt" "$pkg_spec"; }; then
        rm -rf "$VENV"
        [ -z "$previous" ] || mv "$previous" "$VENV"
        die "could not install $pkg_spec${previous:+; the previous installation is unchanged}"
      fi
    fi
    [ -z "$previous" ] || rm -rf "$previous"
    CLIO_VERSION="$package_version"
  fi
fi

need_uv
CLIO_INSTALLED_VERSION=""
if [ -x "$VENV/bin/python" ]; then
  CLIO_INSTALLED_VERSION="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("clio-agent"))' 2>/dev/null || true)"
fi

# ---------- data an older CLIO wrote: moved aside, never deleted ------
# This release cannot open what CLIO 0.9.5.1 or older stored. `store-check` reads the
# store itself and says so in plain words (exit 5: written by 0.9.5.1 or older; 6 or
# 4: cannot tell; 3: CLIO is running). Old data is moved aside after a yes (without
# asking with --reset-old-data or when no terminal can ask); data whose writer cannot
# be told is moved only after a yes. `reset-store --yes` renames it, keeping every byte.

# Whether the person can be asked: output on a terminal and a terminal to read from
# (the script itself may be stdin, as in `curl ... | bash`).
can_ask() { [ -t 1 ] && { : < /dev/tty; } 2>/dev/null; }

# ask_yes QUESTION DEFAULT: 0 for yes; an empty answer is DEFAULT (y or n).
ask_yes() {
  local answer=""
  printf '%s ' "$1"
  IFS= read -r answer < /dev/tty || answer=""
  answer="$(printf '%s' "${answer:-$2}" | tr '[:upper:]' '[:lower:]')"
  case "$answer" in y|yes) return 0 ;; *) return 1 ;; esac
}

move_old_data_aside() {
  local status=0
  "$VENV/bin/python" -m clio_agent.gact.store_reset_command reset --yes || status=$?
  [ "$status" = 0 ] \
    || warn "Your data was not moved (exit $status, the reason is above). CLIO reports it when it starts; 'clio reset-store' moves it later."
}

check_old_data() {
  "$VENV/bin/python" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("clio_agent.gact.store_reset_command") else 3)' 2>/dev/null \
    || return 0  # a release before the check: its store is its own
  say "Checking which CLIO version wrote your data"
  local status=0
  "$VENV/bin/python" -m clio_agent.gact.store_reset_command check || status=$?
  case "$status" in
    0) return 0 ;;
    3) warn "CLIO is running, so your data was not checked. This release cannot open data written by CLIO 0.9.5.1 or older: stop CLIO ('clio stop', or quit CLIO Desktop) and run this installer again, or run 'clio reset-store' if CLIO says your data is from an older version."
       return 0 ;;
    5)
      if [ "$OPT_RESET_OLD_DATA" = 1 ]; then
        say "Moving it aside (--reset-old-data)"
      elif can_ask; then
        if ! ask_yes "Move it aside now? [Y/n]" y; then
          warn "Your data was left in place. CLIO $CLIO_INSTALLED_VERSION cannot open it and says so when it starts. To move it later: 'clio stop', then 'clio reset-store'."
          return 0
        fi
      else
        say "No terminal to ask on: moving it aside (pass --reset-old-data to say so explicitly)"
      fi ;;
    *)
      warn "The installer cannot tell whether your data must be moved aside, so it does not move it on its own."
      [ -z "$PREVIOUS_VERSION" ] || say "The CLIO this install replaced was $PREVIOUS_VERSION."
      if ! { can_ask && ask_yes "Move it aside anyway? [y/N]" n; }; then
        warn "Your data was left in place. If CLIO says it was written by CLIO 0.9.5.1 or older, stop CLIO and run 'clio reset-store'."
        return 0
      fi ;;
  esac
  move_old_data_aside
}
check_old_data

# ---------- upgrade the store: migrations run here, never at boot ------
# Idempotent; a second run is a no-op. A running CLIO refuses it (store_in_use): its
# next boot runs the owed migrations once and reports them in the health rows.
migrate_warning() {  # STATUS: the installer's note for a migrate exit status
  case "$1" in
    0) ;;
    3) warn "store in use by a running CLIO: its next start runs the owed migrations once (or run 'clio stop' then 'clio-agent migrate')" ;;
    5) warn "clio-agent migrate did not run: your data was written by CLIO 0.9.5.1 or older (above); 'clio reset-store' moves it aside" ;;
    *) warn "clio-agent migrate failed (exit $1, see the lines above): CLIO's next start retries the owed migrations once and reports them in 'clio doctor'" ;;
  esac
}
doc_flags=()
# A document step that upgrades the store itself does so while the packages install
# (gact-tui#739: a serial migrate cost 13 s of a cold install); older ones get migrate
# first, as does a document step already running (start_document_install skips it).
docs_migrate=0
if has_module clio_agent.gact.store_migrate_command; then
  if [ -z "$DOC_JOB" ] && has_module clio_agent.runtime.document_install _run_store_migrations; then
    docs_migrate=1
  else
    say "Migrating the CLIO store (clio-agent migrate)"
    migrate_status=0
    "$VENV/bin/python" -m clio_agent.gact.store_migrate_command || migrate_status=$?
    migrate_warning "$migrate_status"
    doc_flags=(--skip-store-migrations)
  fi
else
  say "clio-agent $CLIO_INSTALLED_VERSION has no 'clio-agent migrate'; its boot migrates the store"
fi

if [ -n "$DOC_JOB" ]; then
  docs_status=0
  wait "$DOC_JOB" || docs_status=$?
  DOC_JOB=""
  cat "$DESKTOP_SCRATCH/documents.log" 2>/dev/null || true
  [ -z "$INSTALL_DOWNLOADS" ] || finish_downloads
  rm -rf "$RUNTIME_UNPACK"
  [ "$docs_status" = 0 ] \
    || die_with "$docs_status" "installing the managed packages and Office rendering failed (exit $docs_status); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
  if [ -n "$NODE_JOB" ]; then
    wait "$NODE_JOB" || warn "could not fetch the browser's Node.js driver now ($(tail -n 1 "$DESKTOP_SCRATCH/browser-driver.log" 2>/dev/null)); the first browser action fetches it"
    NODE_JOB=""
  fi
elif has_module clio_agent.runtime.document_install; then
  say 'Installing managed Python/uv and Node/pnpm packages and Office rendering'
  docs_status=0
  # A runtime that cannot wait for a download still arriving gets it once it has landed.
  [ -z "$INSTALL_DOWNLOADS" ] || [ "$DESKTOP_PLAN_WAITS" = 1 ] || finish_downloads
  install_scratch
  ! wants_provider_support || doc_flags+=(--provider-support "$PROVIDER_SUPPORT")
  env ${INSTALL_DOWNLOADS:+"CLIO_INSTALL_DOWNLOADS=$INSTALL_DOWNLOADS"} \
    "$VENV/bin/python" -m clio_agent.runtime.document_install ${doc_flags[@]+"${doc_flags[@]}"} \
    | tee "$DESKTOP_SCRATCH/document_install.out" || docs_status=$?  # pipefail: its status
  [ -z "$INSTALL_DOWNLOADS" ] || finish_downloads
  if [ "$docs_migrate" = 1 ] && [ "$docs_status" = 0 ]; then
    # Its summary (the last line) reports the migration as `clio-agent migrate` would.
    migrate_status="$(tail -n 1 "$DESKTOP_SCRATCH/document_install.out" | "$VENV/bin/python" -I -c '
import json, sys
try:
    m = json.loads(sys.stdin.read()).get("store_migrations") or {}
except (AttributeError, ValueError):
    m = {}
s, r = m.get("status"), m.get("reason")
print(0 if s in ("done", "skipped") else 3 if r == "store_in_use" else 5 if s == "refused" else 1)
' 2>/dev/null || true)"
    [ -z "$migrate_status" ] || migrate_warning "$migrate_status"
  fi
  [ "$docs_status" = 0 ] \
    || die_with "$docs_status" "installing the managed packages and Office rendering failed (exit $docs_status); the cause is in the lines above. Fix it and run this installer again: it resumes from what is already installed."
else
  # Releases before the managed document runtime set it up on their own.
  say "clio-agent $CLIO_INSTALLED_VERSION has no managed document runtime step; skipping it"
fi

# ---------- provision clio-kit MCP runtime ----------------------------
# Started alongside the Python install (start_clio_kit, above); reported here.
finish_clio_kit

# ---------- install the released Web frontend -------------------------
if [ "$OPT_SOURCE" = 0 ]; then
  tag="$(release_tag "${CLIO_INSTALLED_VERSION:-$CLIO_VERSION}")"
  # Web UI bundle (powers `clio --web`): optional, best-effort. The release
  # ships clio-web-<version>.zip containing a clio-web-<version>/ dist dir; we
  # unpack it into $PREFIX/clio-agent/web, which the launcher serves same-origin.
  webver="${tag#v}"
  web_fetched=1
  if [ -n "$WEB_JOB" ] && [ "$WEB_TAG" = "$tag" ]; then
    wait "$WEB_JOB" && mv "$DESKTOP_SCRATCH/clio-web.zip" "$PREFIX/clio-web.zip" || web_fetched=0
    WEB_JOB=""
  else
    web_fetched=0
  fi
  if [ "$web_fetched" = 1 ] || curl -fsSL "$(web_url "$tag")" -o "$PREFIX/clio-web.zip" 2>/dev/null; then
    rm -rf "$PREFIX/_webtmp" "$PREFIX/clio-agent/web"
    # The environment's Python unpacks it: unzip is not installed everywhere.
    if "$VENV/bin/python" -I -m zipfile -e "$PREFIX/clio-web.zip" "$PREFIX/_webtmp"; then
      mkdir -p "$PREFIX/clio-agent/web"
      cp -r "$PREFIX/_webtmp/clio-web-${webver}/." "$PREFIX/clio-agent/web/" 2>/dev/null \
        || cp -r "$PREFIX/_webtmp/." "$PREFIX/clio-agent/web/"
      say "Web UI bundle installed (run: clio --web)"
    else
      warn "could not unpack the web UI bundle for $tag — 'clio --web' disabled until reinstalled with it"
    fi
    rm -rf "$PREFIX/_webtmp" "$PREFIX/clio-web.zip"
  else
    warn "could not download the web UI bundle for $tag ($(web_url "$tag")) — 'clio --web' disabled until reinstalled with it"
  fi
fi

# ---------- launcher + uninstaller ------------------------------------
# When we cloned clio-agent (source mode), the scripts are already on
# disk. In release mode, fetch them from the ref that matches the
# installed PyPI version, unless an explicit installer ref is provided.
launcher_ref="${CLIO_INSTALLER_REF:-}"
if [ -z "$launcher_ref" ] && [ -n "$CLIO_INSTALLED_VERSION" ]; then
  launcher_ref="$(release_tag "$CLIO_INSTALLED_VERSION")"
fi
launcher_ref="${launcher_ref:-main}"
RAW="https://raw.githubusercontent.com/iowarp/clio-agent/${launcher_ref}/install"

# The installed release's own launcher lives in the install; `clio` on PATH pins
# this install's location, so a launcher from any release (whose built-in default
# location may differ) finds it.
LAUNCHER="$BIN_DIR/clio"
RELEASE_LAUNCHER="$PREFIX/libexec/clio"
say "Installing launcher: $LAUNCHER"
mkdir -p "$PREFIX/libexec"
if [ "$OPT_SOURCE" = 1 ]; then
  cp "$SRC/install/clio" "$RELEASE_LAUNCHER"
else
  curl -fsSL "$RAW/clio" -o "$RELEASE_LAUNCHER"
fi
chmod +x "$RELEASE_LAUNCHER"
# An install made for its own data home (an SSH deployment's <root>/user) keeps it,
# so starting it by hand uses the same conversations as the deployment does.
agent_home_line=""
[ -n "${CLIO_AGENT_HOME:-}" ] && agent_home_line="export CLIO_AGENT_HOME=\"\${CLIO_AGENT_HOME:-$CLIO_AGENT_HOME}\""
cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
# CLIO: the installation in $PREFIX, through that installation's own launcher.
export CLIO_PREFIX="\${CLIO_PREFIX:-$PREFIX}"
export CLIO_BIN_DIR="\${CLIO_BIN_DIR:-$BIN_DIR}"
$agent_home_line
exec "$RELEASE_LAUNCHER" "\$@"
LAUNCH
chmod +x "$LAUNCHER"

say "Installing uninstaller: $PREFIX/uninstall.sh"
if [ "$OPT_SOURCE" = 1 ]; then
  cp "$SRC/install/uninstall.sh" "$PREFIX/uninstall.sh"
else
  curl -fsSL "$RAW/uninstall.sh" -o "$PREFIX/uninstall.sh"
fi
chmod +x "$PREFIX/uninstall.sh"

# ---------- finishing notes -------------------------------------------
say "Done."
# macOS defaults to zsh; name the startup file of the shell this person actually uses.
case "$(basename "${SHELL:-}")" in
  zsh)  shell_rc="$HOME/.zshrc"; completion_line="autoload -Uz compinit && compinit; source <(clio completion zsh)" ;;
  bash) if [ "$OS" = darwin ]; then shell_rc="$HOME/.bash_profile"; else shell_rc="$HOME/.bashrc"; fi
        completion_line="source <(clio completion bash)" ;;
  *)    shell_rc="" ;;
esac
case ":$CALLER_PATH:" in
  *":$BIN_DIR:"*) path_step="$BIN_DIR is already on your PATH." ;;
  *) if [ -n "$shell_rc" ]; then
       path_step="Add $BIN_DIR to your PATH: echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> \"$shell_rc\""
     else
       path_step="Add $BIN_DIR to your PATH in your shell's startup file."
     fi ;;
esac
if [ -n "$shell_rc" ]; then
  completion_step="Tab-completion:    echo '$completion_line' >> \"$shell_rc\""
else
  completion_step="Tab-completion:    clio completion bash|zsh prints a script for your shell"
fi
if [ -f "$PREFIX/clio-agent/web/index.html" ]; then
  frontend="Web; Desktop is available with --desktop"
  run_step="Run:   clio (Web), or clio start (headless service)"
else
  # Source installs carry no frontend build; say so instead of pointing at Web.
  frontend="none (source install); Desktop is available with --desktop"
  run_step="Run:   clio start (headless service); for Web set CLIO_WEB_DIR to a built web dist"
fi
# Installed: a release install a source install replaced is no longer needed. When its
# bundled git is the git this install used (a machine without one), that git moves into
# the source environment instead, where CLIO and the next source install find it.
if [ -n "$RELEASE_ASIDE" ]; then
  case "$SOURCE_GIT" in
    "$RELEASE_ASIDE/.venv/git/"*)
      if [ "$SRC" = "$RELEASE_ORIGIN" ] && [ -d "$SRC/.venv" ] && [ ! -e "$SRC/.venv/git" ] \
          && mv "$RELEASE_ASIDE/.venv/git" "$SRC/.venv/git"; then
        SOURCE_GIT="$SRC/.venv/git/${SOURCE_GIT#"$RELEASE_ASIDE/.venv/git/"}"
        write_git_shim
      fi ;;
  esac
  rm -rf "$RELEASE_ASIDE"
fi
clio_src="$(if [ "$OPT_SOURCE" = 1 ]; then echo "source: $SRC ($(git -C "$SRC" rev-parse --short HEAD 2>/dev/null))"; elif [ -n "$RUNTIME_SHA" ]; then echo "release runtime: $CLIO_VERSION"; else echo "PyPI: $CLIO_VERSION"; fi)"
cat <<EOF

Installed to:        $PREFIX
Launcher:            $LAUNCHER
clio-agent:          $clio_src
Frontend:            $frontend

Next steps:
  1. $path_step
  2. $run_step
  3. Manage the server: clio status | clio restart | clio logs
  4. $completion_step
  5. Uninstall:         clio uninstall   (add --purge to drop config)

EOF
