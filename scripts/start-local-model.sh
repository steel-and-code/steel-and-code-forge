#!/usr/bin/env bash
set -euo pipefail

MODEL="${FORGE_LOCAL_MODEL:-Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M}"
ALIAS="${FORGE_LOCAL_ALIAS:-byte-coder}"
HOST="${FORGE_LOCAL_HOST:-127.0.0.1}"
PORT="${FORGE_LOCAL_PORT:-8080}"
GPU_LAYERS="${FORGE_LOCAL_GPU_LAYERS:-99}"

exec llama-server \
  --jinja \
  -hf "$MODEL" \
  -ngl "$GPU_LAYERS" \
  --host "$HOST" \
  --port "$PORT" \
  --alias "$ALIAS"
