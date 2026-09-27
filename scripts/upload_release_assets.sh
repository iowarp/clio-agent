#!/usr/bin/env bash
# Upload files to the GitHub release for a tag -- normally still a DRAFT.
#
# Every clio-bundles.yml uploading job calls this instead of creating the
# release as a side effect of its first upload (the v0.9.4.19 defect: the
# release became "latest" an hour before its updater manifests existed). The
# release itself is created once, as a draft, by the `release` job
# (scripts/github_release.py ensure); `gh release upload` resolves a draft by
# its pending tag name. Publishing happens only in release-check.
#
# Usage: upload_release_assets.sh <tag> <file>...
#
# Fails loud on a missing file (an unmatched glob stays literal and trips
# this) rather than uploading a partial set, and retries transient upload
# failures with a visible ::warning:: per retry -- never a silent retry.
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "::error::usage: upload_release_assets.sh <tag> <file>..." >&2
  exit 2
fi
tag="$1"
shift
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY must be set}"

for f in "$@"; do
  if [ ! -f "$f" ]; then
    echo "::error::release upload: '$f' does not exist (unmatched glob or missing build output)" >&2
    exit 1
  fi
done

max_attempts="${UPLOAD_MAX_ATTEMPTS:-3}"
attempt=1
until gh release upload "$tag" "$@" --clobber --repo "$repo"; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "::error::release upload to $tag failed after $attempt attempts" >&2
    exit 1
  fi
  echo "::warning::release upload to $tag failed (attempt $attempt/$max_attempts); retrying"
  sleep $((attempt * ${UPLOAD_RETRY_BASE_S:-15}))
  attempt=$((attempt + 1))
done
echo "uploaded $# asset(s) to release $tag"
