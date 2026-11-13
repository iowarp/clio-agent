#!/bin/bash
# The ParaView front's processes on this machine that belong to this checkout's stack.
#
#   bash scripts/cloud/checks/apps/paraview_ssh.sh list   # the fronts and their ssh (AQ39)
#   bash scripts/cloud/checks/apps/paraview_ssh.sh kill   # kill the fronts' master ssh (AQ41)
#   bash scripts/cloud/checks/apps/paraview_ssh.sh count  # how many ssh processes they own (AQ43)
#
# A process is ours when one of its ancestors runs from this checkout, so another
# stack's front on the same machine is never listed or killed.
set -u
ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

ours() {  # pid -> 0 when an ancestor's command line names this checkout
  local pid=$1
  while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$ROOT/" && return 0
    pid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null)
  done
  return 1
}

fronts() { for p in $(pgrep -f 'paraview-mcp|paraview_mcp|mcp-server paraview'); do ours "$p" && echo "$p"; done; }
sshs() { for p in $(pgrep -x ssh); do ours "$p" && echo "$p"; done; }

case "${1:-list}" in
  list)
    echo "on this machine, processes of this checkout's stack:"
    for p in $(pgrep -f "$ROOT/" | sort -n); do
      args=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | cut -c1-160)
      case "$args" in *clio-agent*serve*|*uvicorn*|*clio_agent*) echo "  CLIO   $p $args";; esac
    done
    for p in $(fronts); do echo "  front  $p $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-160)"; done
    for p in $(sshs); do echo "  ssh    $p ppid=$(awk '{print $4}' /proc/$p/stat) $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-200)"; done
    ;;
  kill)
    for p in $(sshs); do
      if tr '\0' ' ' < "/proc/$p/cmdline" | grep -q 'ControlMaster=yes'; then
        echo "killing master ssh $p: $(tr '\0' ' ' < /proc/$p/cmdline | cut -c1-200)"
        kill -9 "$p"
      fi
    done
    ;;
  count) sshs | wc -l ;;
  *) echo "usage: $0 list|kill|count"; exit 2 ;;
esac
