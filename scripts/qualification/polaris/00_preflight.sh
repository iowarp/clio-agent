#!/bin/bash
# Step 00: node and project facts BEFORE anything is installed.
#   GPUs + driver, cgroup pids limit, node-local scratch, Eagle project space AND inodes
#   (the Delta campaign hit a project-wide inode-quota crisis), home quota, proxy reach.
# Fails (exit 1) when the project lacks INODE_MIN free inodes or SPACE_MIN_GB free space,
# or when fewer GPUs than VLLM_TP are visible. FORCE=1 records the shortfall and continues.
set -uo pipefail
# shellcheck source=common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

problems=()
facts=()
add() { facts+=("$1=$2"); }

add pbs_job "\"${PBS_JOBID:-}\""
add node_scratch "\"$NODE_SCRATCH\""

# --- GPUs -------------------------------------------------------------------------
gpus=0; driver=""; cuda=""
if command -v nvidia-smi >/dev/null 2>&1; then
  gpus=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)
  driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
  cuda=$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' | awk '{print $3}')
  busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || true)
  nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv > "$JOB_EVIDENCE/gpus.csv" 2>/dev/null || true
  add gpu_processes "${busy:-0}"
fi
add gpus "$gpus"; add driver "\"$driver\""; add driver_cuda "\"$cuda\""
[ "$gpus" -ge "$VLLM_TP" ] || problems+=("only $gpus GPU(s) visible, VLLM_TP=$VLLM_TP")
# vLLM 0.27 wheels need a CUDA 12.8+ capable driver (>= 570). Older: set CUDA_COMPAT_DIR.
major="${driver%%.*}"
if [ -n "$major" ] && [ "$major" -lt 570 ] 2>/dev/null; then
  problems+=("driver $driver (CUDA $cuda) may be older than vLLM 0.27's CUDA runtime; set CUDA_COMPAT_DIR to a cuda-compat lib dir or expect 'driver insufficient'")
fi

# --- cgroup pids limit (Aurora: pids.max=8192 aborted ZMQ threads) -------------------
pids_max=""
cg=$(awk -F: '$1=="0"{print $3}' /proc/self/cgroup 2>/dev/null)
for f in "/sys/fs/cgroup$cg/pids.max" "/sys/fs/cgroup/pids$(awk -F: '$2=="pids"{print $3}' /proc/self/cgroup 2>/dev/null)/pids.max"; do
  [ -r "$f" ] && pids_max=$(cat "$f") && break
done
add pids_max "\"${pids_max:-unknown}\""
add ulimit_u "\"$(ulimit -u)\""; add ulimit_n "\"$(ulimit -n)\""
if [ -n "$pids_max" ] && [ "$pids_max" != max ] && [ "$pids_max" -lt 4096 ] 2>/dev/null; then
  problems+=("cgroup pids.max=$pids_max is low for vLLM TP workers + Flowcept + CLIO threads")
fi

# --- node-local scratch --------------------------------------------------------------
add scratch_free_gb "$(df -Pk "$NODE_SCRATCH" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}')"

# --- Eagle project: space and inodes ---------------------------------------------------
proj_dir="/eagle/$EAGLE_PROJECT"
files_used=""; files_limit=""; kb_used=""; kb_limit=""
if command -v lfs >/dev/null 2>&1 && [ -d "$proj_dir" ]; then
  projid=$(lfs project -d "$proj_dir" 2>/dev/null | awk '{print $1}')
  if [ -n "$projid" ] && [ "$projid" != 0 ]; then
    read -r kb_used kb_limit files_used files_limit < <(lfs quota -q -p "$projid" /eagle 2>/dev/null \
      | awk '{gsub(/\*/,""); for(i=1;i<=NF;i++) if($i ~ /^\//){
          kl = ($(i+3) > 0) ? $(i+3) : $(i+2); fl = ($(i+7) > 0) ? $(i+7) : $(i+6)
          print $(i+1), kl, $(i+5), fl; exit}}')
  fi
  lfs quota -h -p "${projid:-0}" /eagle > "$JOB_EVIDENCE/eagle-quota.txt" 2>&1 || true
fi
command -v myprojectquotas >/dev/null 2>&1 && myprojectquotas > "$JOB_EVIDENCE/myprojectquotas.txt" 2>&1
command -v myquota >/dev/null 2>&1 && myquota > "$JOB_EVIDENCE/myquota.txt" 2>&1
df -Pi "$ROOT" > "$JOB_EVIDENCE/df-inodes.txt" 2>/dev/null || true
df -Ph "$ROOT" > "$JOB_EVIDENCE/df-space.txt" 2>/dev/null || true
if [ -n "$files_limit" ] && [ "$files_limit" -gt 0 ] 2>/dev/null; then
  free_inodes=$((files_limit - files_used))
  free_gb=$(( (kb_limit - kb_used) / 1048576 ))
  add project_files_used "$files_used"; add project_files_limit "$files_limit"
  add project_free_inodes "$free_inodes"; add project_free_gb "$free_gb"
  [ "$free_inodes" -ge "$INODE_MIN" ] || problems+=("project $EAGLE_PROJECT has $free_inodes free inodes (< INODE_MIN=$INODE_MIN): uninstall old hosts' services or clean caches first")
  [ "$free_gb" -ge "$SPACE_MIN_GB" ] || problems+=("project $EAGLE_PROJECT has ${free_gb} GB free (< SPACE_MIN_GB=$SPACE_MIN_GB)")
else
  add project_quota "\"unreadable (see eagle-quota.txt / df); checked df only\""
  df_free_inodes=$(df -Pi "$ROOT" 2>/dev/null | awk 'NR==2{print $4}')
  add df_free_inodes "\"${df_free_inodes:-unknown}\""
fi
# What this kit already holds (inodes), so a census is cheap to compare across jobs.
if [ -d "$ROOT" ] && [ "${SKIP_CENSUS:-0}" != 1 ]; then
  for d in "$ROOT"/*/; do
    printf '%s %s\n' "$(timeout 300 find "$d" -xdev 2>/dev/null | wc -l)" "$d"
  done > "$JOB_EVIDENCE/inode-census.txt" 2>/dev/null
fi

# --- proxy reachability ------------------------------------------------------------------
for url in https://github.com https://pypi.org/simple/ https://huggingface.co https://drive.usercontent.google.com https://nodejs.org/dist/; do
  code=$(curl -sS -o /dev/null -m 25 -w '%{http_code}' "$url" 2>/dev/null || echo 000)
  add "reach_$(echo "$url" | sed 's#https://##; s#[^a-z0-9]#_#g')" "\"$code\""
  case "$code" in 2*|3*|4*) ;; *) problems+=("$url unreachable (HTTP $code) through ${https_proxy:-no proxy}") ;; esac
done

# --- Apptainer (fallback only) --------------------------------------------------------------
# --- native Flowcept prerequisites (prebuilt Valkey + MongoDB's verified build; nothing is compiled) --
add os_release "\"$(. /etc/os-release 2>/dev/null; echo "${ID:-?} ${VERSION_ID:-?}")\""
glibc=$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')
add glibc "\"${glibc:-unknown}\""
openssl11=$(/sbin/ldconfig -p 2>/dev/null | grep -c 'libssl.so.1.1 ' || true)
add libssl_1_1 "${openssl11:-0}"
if [ "$FLOWCEPT_VARIANT" = native ]; then
  # Valkey's prebuilt Linux binary needs glibc 2.34+ (e.g. SLES 15 SP6, not SP5).
  case "$(printf '%s\n2.34\n' "${glibc:-0}" | sort -V | head -1)" in
    2.34) ;;
    *) problems+=("native Flowcept needs glibc 2.34+ for Valkey's prebuilt binary; this host has ${glibc:-unknown}. Use FLOWCEPT_VARIANT=managed (Apptainer databases)") ;;
  esac
  # Only MongoDB's suse15 build links OpenSSL 1.1; the other verified builds use the host's own.
  case "$(. /etc/os-release 2>/dev/null; echo "${ID:-}")" in
    sles|sled|sle_hpc|opensuse-leap)
      [ "${openssl11:-0}" -gt 0 ] \
        || problems+=("native Flowcept's MongoDB needs libssl.so.1.1 (SUSE package libopenssl1_1); else FLOWCEPT_VARIANT=managed") ;;
  esac
fi

load_modules
add apptainer "\"$(command -v apptainer >/dev/null 2>&1 && apptainer --version 2>/dev/null || echo none)\""
add system_python "\"$(python3 -V 2>&1)\""

status=ok
if [ ${#problems[@]} -gt 0 ]; then
  printf 'PROBLEM: %s\n' "${problems[@]}" >&2
  status=failed
  [ "${FORCE:-0}" = 1 ] && status=forced
fi
problems_json=$(printf '%s\n' "${problems[@]+"${problems[@]}"}" | "$PY" -c 'import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))')
receipt 00-preflight "$status" "${facts[@]}" "problems=$problems_json"
[ "$status" != failed ]
