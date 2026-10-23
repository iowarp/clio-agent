#!/bin/bash
# Runs the Notebook theme qualification (docs/design/theme/notebook-theme-plan.md,
# rows T2 to T7) against a running cloud stack, at 1440x900 and 390x844.
# Results land in .cloud-run/theme-<row>-<size>.json and screenshots in
# .cloud-run/shots/. Start the stack first: bash scripts/cloud/stack.sh restart
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
cd "$ROOT" || exit 1
CHECKS=scripts/cloud/checks
status=0
for size in "wide 1440 900" "phone 390 844"; do
  set -- $size
  for row in t2_switch t3_scheme t4_screens t5_contrast t7_debug; do
    [ "$row" = t5_contrast ] && [ "$1" = phone ] && continue  # token values do not depend on width
    out=".cloud-run/theme-${row%%_*}-$1.json"
    node scripts/cloud/ui_check.mjs --label "${row%%_*}-$1" --width "$2" --height "$3" \
      --steps "$CHECKS/theme_$row.mjs" > "$out" 2>&1 || status=1
    echo "${row%%_*} $1: $out"
  done
done
(cd external/gact-tui && node scripts/check_theme_colors.mjs) || status=1  # T6
exit $status
