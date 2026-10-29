#!/bin/bash
# Print (and record in $ROOT/run/ENDPOINT.txt) how the user's desktop reaches this node's CLIO.
# CLIO listens on the compute node's loopback only. The recommended route forwards through a
# login node over a user-only UNIX socket, so no TCP port is opened on the shared login node.
set -uo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"
port="$("${KIT_PY[@]}" endpoint | sed 's#.*:##')"
node="$(hostname -f 2>/dev/null || hostname)"
login="${POLARIS_LOGIN:-polaris.alcf.anl.gov}"
sock="/tmp/clio-$USER-$JOB_TAG.sock"
local_port="${LOCAL_PORT:-8100}"
tcp_port=$(( 20000 + RANDOM % 20000 ))
out="$ROOT/run/ENDPOINT.txt"
text="CLIO on Polaris: job ${PBS_JOBID:-none}, node $node, loopback port $port (written $(date -Is)).

From YOUR machine (one MFA prompt for the login node; the inner hop uses ALCF host keys):
  ssh -t -L $local_port:$sock $USER@$login \\
    \"ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes -L $sock:127.0.0.1:$port $node\"
Then open http://127.0.0.1:$local_port (the CLIO web UI is served by CLIO itself), or point the
CLIO desktop app at http://127.0.0.1:$local_port. Stop with Ctrl-C (the socket is removed).

Fallback if UNIX-socket forwarding is refused (TCP on the login node's loopback, which other
users of that login node could reach while it is open):
  ssh -t -L $local_port:127.0.0.1:$tcp_port $USER@$login \\
    \"ssh -N -o ExitOnForwardFailure=yes -L $tcp_port:127.0.0.1:$port $node\"

End the batch session early:  touch $ROOT/run/END-$JOB_TAG"
if [ "$DRY_RUN" = 1 ]; then echo "$text"; exit 0; fi
printf '%s\n' "$text" | tee "$out"
