#!/usr/bin/env bash
# Developer ID signing keychain for macOS release jobs.
#
#   bash scripts/macos_signing_keychain.sh setup    # import, verify, export CLIO_SIGNING_KEYCHAIN
#   bash scripts/macos_signing_keychain.sh cleanup  # delete the keychain (always run)
#
# setup imports APPLE_CERTIFICATE (base64 .p12) and Apple's Developer ID G2
# intermediate into a throwaway keychain, then requires
# `security find-identity -v -p codesigning` to list APPLE_SIGNING_IDENTITY as
# a valid identity of APPLE_TEAM_ID and the notary service to accept the Apple
# Account credentials before the build starts. Every Apple credential a release needs
# must be present: a macOS release is never built ad hoc or unnotarized.
set -euo pipefail

G2_URL="https://www.apple.com/certificateauthority/DeveloperIDG2CA.cer"
G2_SHA256="f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a"
KEYCHAIN="${RUNNER_TEMP:?}/clio-signing.keychain-db"

# Ask Apple's notary service to authenticate before the long build, and say which
# credential looks wrong without printing any of them.
check_notary_credentials() {
  local name value
  for name in APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID; do
    value="$(printenv "$name")"
    if [ "$value" != "$(printf '%s' "$value" | tr -d '[:space:]')" ]; then
      echo "::warning::$name contains whitespace or a newline"
    fi
  done
  [[ "$APPLE_ID" == *@*.* ]] || echo "::warning::APPLE_ID is not an Apple Account email address"
  [[ "$APPLE_PASSWORD" =~ ^[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}$ ]] \
    || echo "::warning::APPLE_PASSWORD is not shaped like an app-specific password (xxxx-xxxx-xxxx-xxxx)"
  [[ "$APPLE_TEAM_ID" =~ ^[A-Z0-9]{10}$ ]] || echo "::warning::APPLE_TEAM_ID is not a 10-character team ID"

  local output
  if ! output="$(xcrun notarytool history --apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" --output-format json 2>&1)"; then
    echo "$output" >&2
    echo "::error::Apple's notary service rejected APPLE_ID / APPLE_PASSWORD / APPLE_TEAM_ID; use an app-specific password from account.apple.com for an Apple Account in team APPLE_TEAM_ID" >&2
    exit 1
  fi
  echo "Notary service accepted the Apple Account credentials."
}

setup() {
  local missing=() name
  for name in APPLE_CERTIFICATE APPLE_CERTIFICATE_PASSWORD APPLE_SIGNING_IDENTITY \
    APPLE_ID APPLE_PASSWORD APPLE_TEAM_ID; do
    [ -n "$(printenv "$name" || true)" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "::error::macOS release signing needs ${missing[*]} (release-mac-signing environment)" >&2
    exit 1
  fi
  case "$APPLE_SIGNING_IDENTITY" in
    "Developer ID Application: "*"($APPLE_TEAM_ID)") ;;
    *)
      echo "::error::APPLE_SIGNING_IDENTITY is not a Developer ID Application identity of team APPLE_TEAM_ID" >&2
      exit 1
      ;;
  esac

  local password certificate intermediate
  password="$(openssl rand -hex 24)"
  certificate="$RUNNER_TEMP/clio-signing.p12"
  intermediate="$RUNNER_TEMP/DeveloperIDG2CA.cer"
  security create-keychain -p "$password" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"
  security unlock-keychain -p "$password" "$KEYCHAIN"

  printf '%s' "$APPLE_CERTIFICATE" | base64 --decode >"$certificate"
  security import "$certificate" -k "$KEYCHAIN" -P "$APPLE_CERTIFICATE_PASSWORD" \
    -f pkcs12 -T /usr/bin/codesign -T /usr/bin/security >/dev/null
  rm -f "$certificate"

  curl -fsSL --retry 3 "$G2_URL" -o "$intermediate"
  echo "$G2_SHA256  $intermediate" | shasum -a 256 --check --quiet
  # A .p12 exported with its chain already carries the intermediate.
  if ! security find-certificate -Z -a "$KEYCHAIN" | grep -qi "SHA-256 hash: $G2_SHA256"; then
    security add-certificates -k "$KEYCHAIN" "$intermediate"
  fi

  # Let codesign use the key without a UI prompt, and put the keychain on the
  # user search list so codesign and Tauri's bundler find the identity.
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$password" "$KEYCHAIN" >/dev/null
  # shellcheck disable=SC2046 # one word per existing keychain path
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | xargs)

  local identities
  identities="$(security find-identity -v -p codesigning "$KEYCHAIN")"
  echo "$identities"
  if ! grep -qF "\"$APPLE_SIGNING_IDENTITY\"" <<<"$identities"; then
    echo "::error::find-identity does not list APPLE_SIGNING_IDENTITY as a valid codesigning identity" >&2
    exit 1
  fi
  echo "CLIO_SIGNING_KEYCHAIN=$KEYCHAIN" >>"${GITHUB_ENV:?}"
  check_notary_credentials
}

cleanup() {
  rm -f "$RUNNER_TEMP/clio-signing.p12"
  if [ -f "$KEYCHAIN" ]; then
    security delete-keychain "$KEYCHAIN"
  fi
}

case "${1:-}" in
  setup) setup ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 setup|cleanup" >&2
    exit 2
    ;;
esac
