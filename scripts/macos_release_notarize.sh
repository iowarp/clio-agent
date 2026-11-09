#!/usr/bin/env bash
# Notarize the release DMG and prove what a macOS user receives is release-signed.
#
#   bash scripts/macos_release_notarize.sh notarize-dmg BUNDLE_DIR
#   bash scripts/macos_release_notarize.sh verify BUNDLE_DIR bundled|lite
#
# BUNDLE_DIR is Tauri's target/<triple>/release/bundle. `tauri build` has
# already signed the app and sidecar with hardened runtime, notarized the app
# and stapled it before packing the DMG and the updater .app.tar.gz (APPLE_ID,
# APPLE_PASSWORD and APPLE_TEAM_ID in its environment). notarize-dmg signs the
# DMG with a secure timestamp, notarizes it and staples its ticket; the DMG's
# bytes change, so checksums are computed afterwards. verify checks the app,
# the DMG, every runtime Mach-O and the app extracted from the updater payload,
# whose minisign signature must still verify against the brand key.
set -euo pipefail

: "${APPLE_SIGNING_IDENTITY:?}" "${APPLE_TEAM_ID:?}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRAND_CONFIG="$REPO/branding/clio/tauri.clio.conf.json"

one() {
  local pattern="$1" dir="$2" found
  found="$(find "$dir" -maxdepth 1 -name "$pattern" -print)"
  if [ -z "$found" ] || [ "$(printf '%s\n' "$found" | wc -l | tr -d ' ')" -ne 1 ]; then
    echo "::error::expected exactly one $pattern in $dir, found: ${found:-none}" >&2
    exit 1
  fi
  printf '%s\n' "$found"
}

notarytool() {
  xcrun notarytool "$@" --apple-id "${APPLE_ID:?}" --password "${APPLE_PASSWORD:?}" \
    --team-id "$APPLE_TEAM_ID"
}

notarize_dmg() {
  local dmg result status id
  dmg="$(one '*.dmg' "$1/dmg")"
  codesign --force --sign "$APPLE_SIGNING_IDENTITY" --timestamp \
    ${CLIO_SIGNING_KEYCHAIN:+--keychain "$CLIO_SIGNING_KEYCHAIN"} "$dmg"
  result="$RUNNER_TEMP/notary-dmg.json"
  notarytool submit "$dmg" --wait --timeout 60m --output-format json >"$result"
  status="$(jq -r .status "$result")"
  id="$(jq -r .id "$result")"
  echo "notarization $id: $status"
  notarytool log "$id" "$RUNNER_TEMP/notary-dmg-log.json" || true
  [ -f "$RUNNER_TEMP/notary-dmg-log.json" ] && jq '{status, statusSummary, issues}' "$RUNNER_TEMP/notary-dmg-log.json"
  if [ "$status" != Accepted ]; then
    echo "::error::Apple notarization of $(basename "$dmg") returned $status" >&2
    exit 1
  fi
  xcrun stapler staple "$dmg"
  xcrun stapler validate "$dmg"
}

# Release identity, hardened runtime and a secure timestamp on one signed path.
check_code() {
  local path="$1" details
  codesign --verify --strict --verbose=2 "$path"
  details="$(codesign -d --verbose=4 "$path" 2>&1)"
  grep -q "^Authority=Developer ID Application: " <<<"$details" \
    || { echo "::error::$path is not signed with a Developer ID Application identity" >&2; return 1; }
  grep -q "^TeamIdentifier=$APPLE_TEAM_ID\$" <<<"$details" \
    || { echo "::error::$path is not signed by team $APPLE_TEAM_ID" >&2; return 1; }
  grep -q "^Timestamp=" <<<"$details" \
    || { echo "::error::$path has no secure timestamp" >&2; return 1; }
  grep -Eq "flags=0x[0-9a-f]+\([^)]*runtime" <<<"$details" \
    || { echo "::error::$path lacks the hardened runtime" >&2; return 1; }
}

# Everything Gatekeeper checks on a downloaded app, plus every nested binary.
check_app() {
  local app="$1" variant="$2" binary assessment
  codesign --verify --deep --strict --verbose=2 "$app"
  check_code "$app"
  for binary in "$app"/Contents/MacOS/*; do
    check_code "$binary"
  done
  assessment="$(spctl --assess --type execute -vv "$app" 2>&1)"
  echo "$assessment"
  grep -q "source=Notarized Developer ID" <<<"$assessment" \
    || { echo "::error::Gatekeeper does not accept $app as notarized" >&2; return 1; }
  xcrun stapler validate "$app"
  if [ "$variant" = bundled ]; then
    uv run --no-project --python 3.14 "$REPO/scripts/macos_sign_runtime.py" verify \
      --runtime "$app/Contents/Resources/gact-runtime" --team-id "$APPLE_TEAM_ID"
  fi
}

verify() {
  local root="$1" variant="$2" app dmg payload extracted assessment dmg_details
  app="$(one '*.app' "$root/macos")"
  dmg="$(one '*.dmg' "$root/dmg")"
  payload="$(one '*.app.tar.gz' "$root/macos")"

  echo "== app"
  check_app "$app" "$variant"

  echo "== DMG"
  dmg_details="$(codesign -d --verbose=4 "$dmg" 2>&1)"
  grep -q "^TeamIdentifier=$APPLE_TEAM_ID\$" <<<"$dmg_details" \
    || { echo "::error::$dmg is not signed by team $APPLE_TEAM_ID" >&2; exit 1; }
  assessment="$(spctl --assess --type open --context context:primary-signature -vv "$dmg" 2>&1)"
  echo "$assessment"
  grep -q "source=Notarized Developer ID" <<<"$assessment" \
    || { echo "::error::Gatekeeper does not accept $dmg as notarized" >&2; exit 1; }
  xcrun stapler validate "$dmg"

  echo "== updater payload"
  extracted="$RUNNER_TEMP/updater-payload"
  rm -rf "$extracted"
  mkdir -p "$extracted"
  tar -xzf "$payload" -C "$extracted"
  check_app "$(one '*.app' "$extracted")" "$variant"
  uv run --no-project --python 3.14 --with cryptography \
    "$REPO/scripts/verify_tauri_update_signature.py" --config "$BRAND_CONFIG" \
    "$payload" "$payload.sig"
  rm -rf "$extracted"
}

case "${1:-}" in
  notarize-dmg) notarize_dmg "${2:?bundle dir}" ;;
  verify) verify "${2:?bundle dir}" "${3:?bundled or lite}" ;;
  *)
    echo "usage: $0 notarize-dmg BUNDLE_DIR | verify BUNDLE_DIR bundled|lite" >&2
    exit 2
    ;;
esac
