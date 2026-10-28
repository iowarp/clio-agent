#!/bin/bash
# Submit polaris_job.pbs with the account, queue and walltime from the kit settings.
#   bash scripts/qualification/polaris/submit.sh [extra qsub args]
# Variables passed into the job: KIT_DIR, CLIO_POLARIS_ENV, RUN_OPAL, HOLD, FORCE, STAGE_UP.
set -euo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
command -v qsub >/dev/null || [ "$DRY_RUN" = 1 ] || die "qsub not found: run this on a Polaris login node"
if [ "$DRY_RUN" != 1 ] && ! qstat -Q "$PBS_QUEUE" >/dev/null 2>&1; then
  die "queue '$PBS_QUEUE' is not available here (qstat -Q lists the queues); set PBS_QUEUE"
fi
vars="KIT_DIR=$KIT_DIR,CLIO_POLARIS_ENV=$CLIO_POLARIS_ENV,RUN_OPAL=${RUN_OPAL:-1},HOLD=${HOLD:-1},FORCE=${FORCE:-0},STAGE_UP=${STAGE_UP:-1},SPOTTER_MODE=${SPOTTER_MODE:-shared},RUN_OPAL_BROAD=${RUN_OPAL_BROAD:-1}"
run mkdir -p "$LOGS"
run qsub -A "$PBS_ACCOUNT" -q "$PBS_QUEUE" -l walltime="$PBS_WALLTIME" \
  -l select=1:system=polaris -l filesystems=home:eagle -o "$LOGS/" -v "$vars" "$@" \
  "$KIT_DIR/polaris_job.pbs"
