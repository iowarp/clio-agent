#!/bin/bash
# Step 05: OPAL through CLIO, after a passing attention smoke.
#   setup: workspace "opal-polaris" + the staged export as a READ-ONLY local source +
#          two fresh sessions (APPL-CORE agent, SPOTTER approval mode, the bound Granite model)
#   (1) Bogdan's chain OPAL_CHAIN_ID, round by round, once per condition in OPAL_CONDITIONS
#       (A = plain; B = the round-5 injection, exp32-chain-0020-hard only), each in its own
#       fresh session: python -m scripts.qualification.opal_chain_run (prepares the inputs,
#       runs the rounds, writes OUT/attention-<cond>.csv in attn_analytics.py columns and
#       OUT/score-<cond>.json). OUT is operator-only: it holds the correct answers.
#   (2) the broad SPOTTER investigation (scripts/qualification/beta3_opal_run.py over
#       docs/qualification/beta3/opal-live-inputs.json)
# Both drivers keep resumable receipts under $EVIDENCE/opal/: rerunning continues where a
# previous job stopped (same endpoint, session and inputs). History is the model's own answers.
set -uo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
O="$EVIDENCE/opal"; run mkdir -p "$O"
smoke="$JOB_EVIDENCE/09-attention-smoke.json"
if [ "$DRY_RUN" != 1 ] && ! grep -q '"status": "ok"' "$smoke" 2>/dev/null && [ "${SKIP_SMOKE_GATE:-0}" != 1 ]; then
  die "no passing attention smoke in this job ($smoke); run 03_attention_smoke.sh first"
fi
"${KIT_PY[@]}" opal-setup || die "OPAL setup (see $JOB_EVIDENCE/failed-opal-setup.json)"
endpoint="$("${KIT_PY[@]}" endpoint)"
broad_sid="$("${KIT_PY[@]}" session-id broad)"
deadline="${KIT_DEADLINE:-$(( $(date +%s) + 86400 ))}"
left() { local s=$(( deadline - $(date +%s) )); [ "$s" -gt 60 ] && echo "$s" || echo 60; }
[ "$DRY_RUN" = 1 ] || cd "$CLIO_SRC" || die "no $CLIO_SRC"
rc_chain=skipped; rc_broad=skipped
# Under DRY_RUN nothing is created, so the log only goes to the terminal.
keep_log() { if [ "$DRY_RUN" = 1 ]; then cat; else tee -a "$1"; fi; }

if [ "$RUN_OPAL_CHAIN" = 1 ]; then
  if [ -f scripts/qualification/opal_chain_run.py ] || [ "$DRY_RUN" = 1 ]; then
    rc_chain=0
    for cond in $OPAL_CONDITIONS; do
      sid="$("${KIT_PY[@]}" session-id "chain_$cond")"
      out="$OPERATOR_DIR/chain-$OPAL_CHAIN_ID-$cond"   # operator-only: holds correct answers
      # The driver creates $out itself and refuses an existing one (no overwrite of another
      # run's evidence), so only the operator parent is made here.
      run mkdir -p "$OPERATOR_DIR"; run chmod 700 "$OPERATOR_DIR"
      log "chain $OPAL_CHAIN_ID condition $cond -> fresh session $sid (out $out)"
      run timeout "$(left)" uv run --frozen --no-sync python -m scripts.qualification.opal_chain_run \
        --endpoint "$endpoint" --session "$sid" --chains "$OPAL_CHAINS_CSV" \
        --chain-id "$OPAL_CHAIN_ID" --condition "$cond" --out "$out" \
        --timeout "$OPAL_TURN_TIMEOUT_S" 2>&1 | keep_log "$O/chain-$OPAL_CHAIN_ID-$cond.log"
      rc=${PIPESTATUS[0]}; [ "$rc" = 0 ] || rc_chain=$rc
      "${KIT_PY[@]}" opal-report "chain_$cond" || true
      [ "$DRY_RUN" = 1 ] || ls -l "$out" > "$O/chain-$OPAL_CHAIN_ID-$cond.outputs.txt" 2>&1
    done
  else
    rc_chain=driver_missing; log "WARNING: scripts/qualification/opal_chain_run.py is not on this branch; chain run skipped"
  fi
fi

if [ "$RUN_OPAL_BROAD" = 1 ]; then
  log "broad SPOTTER investigation -> session $broad_sid"
  run timeout "$(left)" uv run --frozen --no-sync python scripts/qualification/beta3_opal_run.py \
    --endpoint "$endpoint" --session "$broad_sid" \
    --input docs/qualification/beta3/opal-live-inputs.json \
    --receipt "$O/broad.json" --timeout "$OPAL_TURN_TIMEOUT_S" 2>&1 | keep_log "$O/broad.log"
  rc_broad=${PIPESTATUS[0]}
  "${KIT_PY[@]}" opal-report broad || true
fi

status=ok
[ "$rc_chain" = 0 ] || [ "$rc_chain" = skipped ] || status=failed
[ "$rc_broad" = 0 ] || [ "$rc_broad" = skipped ] || status=failed
receipt 12-opal-run "$status" "chain_exit=\"$rc_chain\"" "broad_exit=\"$rc_broad\"" \
  "chain_outputs=\"$OPERATOR_DIR/chain-$OPAL_CHAIN_ID-<condition>\"" "conditions=\"$OPAL_CONDITIONS\"" \
  "broad_receipt=\"$O/broad.json\"" "broad_session=\"$broad_sid\""
[ "$status" = ok ]
