#!/usr/bin/env bash
# shellcheck shell=bash
#
# CLIO homelab provider profiles.
# Source this file, then switch with:
#   clio_homelab_use dynamo-lms
#   clio_homelab_use mini-llama
#   clio_homelab_use dynamo-ollama

_clio_homelab_set_common() {
  export CLIO_LM_API_BASE="$1"
  export CLIO_LM_MODEL="$2"
  export CLIO_LM_API_KEY="$3"
}

clio_homelab_use() {
  local profile="${1:-}"

  case "$profile" in
    dynamo-lms)
      export CLIO_LM_PROVIDER="lm_studio"
      _clio_homelab_set_common \
        "http://192.168.86.143:1234/v1" \
        "nemotron-cascade-2-30b-a3b-i1" \
        "lm-studio"
      ;;
    mini-llama)
      export CLIO_LM_PROVIDER="lm_studio"
      _clio_homelab_set_common \
        "http://192.168.86.141:8080/v1" \
        "Qwen3.5-35B-A3B-UD-Q4_K_XL" \
        "llama"
      ;;
    dynamo-ollama)
      export CLIO_LM_PROVIDER="ollama"
      _clio_homelab_set_common \
        "http://192.168.86.143:11434/v1" \
        "nemotron-3-nano:30b" \
        "ollama"
      ;;
    status)
      clio_homelab_status
      return 0
      ;;
    ""|help|-h|--help)
      cat <<'EOF'
Usage:
  clio_homelab_use <profile>

Profiles:
  dynamo-lms      -> DYNAMO LM Studio (nemotron-cascade-2-30b-a3b-i1)
  mini-llama      -> MINI llama.cpp server (Qwen3.5-35B-A3B-UD-Q4_K_XL)
  dynamo-ollama   -> DYNAMO Ollama (nemotron-3-nano:30b)
  status          -> print current CLIO_LM_* values
EOF
      return 0
      ;;
    *)
      echo "Unknown profile: $profile" >&2
      echo "Run: clio_homelab_use help" >&2
      return 1
      ;;
  esac

  clio_homelab_status
}

clio_homelab_status() {
  printf "CLIO_LM_PROVIDER=%s\n" "${CLIO_LM_PROVIDER:-}"
  printf "CLIO_LM_API_BASE=%s\n" "${CLIO_LM_API_BASE:-}"
  printf "CLIO_LM_MODEL=%s\n" "${CLIO_LM_MODEL:-}"
  printf "CLIO_LM_API_KEY=%s\n" "${CLIO_LM_API_KEY:-}"
}
